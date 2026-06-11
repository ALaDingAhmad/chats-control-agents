"""Per-alias daemon 生命周期通用工具。

每个 backend 的 daemon（`backends/<name>/daemon.py`）都做同一组"账本"动作：

  1. 从 CLI 拿 alias + cwd
  2. 准备 `chat_sessions/<alias>/` 和日志
  3. 写 `meta.json`（含 `backend` 字段，让 spawn 知道下次起哪个 daemon）
  4. spawn 一个下游子进程，把它的 PID 记到 `spawned_pids.jsonl`
  5. 装 atexit + SIGINT 清理：杀子进程、关日志、meta 标 offline

这些事跟下游 agent 是什么、daemon 在不在消息路径上**没关系**——
所以抽出来公共调用。剩下的事（怎么 spawn、怎么知道下游 ready、怎么处理
下游异常、消息流转）由 backend 自己的 daemon 写，本模块不插手。

设计原则见 `docs/BACKEND-DESIGN.md`。
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .paths import ALIAS_RE, session_dir
from .sessions import load_meta_for, make_alias_for_cwd, save_meta_for


# ── CLI 解析 ──────────────────────────────────────────────────────────────
def parse_cli_args(default_cwd: str | Path | None = None) -> tuple[str, str | None]:
    """解析 `<alias> [<cwd>]` 或 `<cwd>` 两种形式。

    用法（每个 backend 的 daemon.py main 顶端调用）：

        from chats_control_agents.core.daemon_lifecycle import parse_cli_args
        alias, cwd_arg = parse_cli_args(default_cwd=Path.home())

    规则：
      - 第一个参数看起来像目录路径（含 / 或 \\ 且 isdir）→ 当 cwd-only
      - 否则第一个参数当 alias（必须过 ALIAS_RE），第二个可选当 cwd
      - alias 缺省时用 `make_alias_for_cwd(cwd 或 default_cwd)` 生成

    返回 (alias, cwd_arg)。cwd_arg 可能是 None——具体怎么 fallback 由
    `resolve_spawn_cwd` 决定。
    """
    args = sys.argv[1:]
    alias: str | None = None
    cwd: str | None = None

    if args and Path(args[0]).is_dir() and ("/" in args[0] or "\\" in args[0]):
        cwd = args[0]
    elif args:
        if not ALIAS_RE.match(args[0]):
            print(
                f"ERROR: invalid alias '{args[0]}'. "
                f"allowed: a-zA-Z0-9_- and Chinese, 1-32 chars",
                file=sys.stderr,
            )
            sys.exit(2)
        alias = args[0]
        if len(args) >= 2 and Path(args[1]).is_dir():
            cwd = args[1]

    if alias is None:
        naming_cwd = cwd or (str(default_cwd) if default_cwd else str(Path.home()))
        alias = make_alias_for_cwd(naming_cwd)

    return alias, cwd


def resolve_spawn_cwd(
    cli_cwd: str | None,
    alias: str,
    backend_default: str | Path | None = None,
) -> str:
    """决定下游子进程的 cwd。

    优先级：CLI 参数 > meta.json 历史保存的 cwd > backend 默认 > $HOME 兜底。

    backend_default 是每个 backend 自己的兜底值（claude_code 历史用
    claude-code-account-switch，hermes_acp 用 $HOME 即可）。
    """
    if cli_cwd and Path(cli_cwd).is_dir():
        return cli_cwd

    prev = load_meta_for(alias) or {}
    prev_cwd = prev.get("cwd")
    if prev_cwd and Path(prev_cwd).is_dir():
        return prev_cwd

    if backend_default and Path(backend_default).is_dir():
        return str(backend_default)

    return str(Path.home())


# ── 生命周期上下文 ────────────────────────────────────────────────────────
@dataclass
class DaemonContext:
    """一个 daemon 进程从启动到退出共享的状态。"""

    alias: str
    cwd: str
    backend: str
    session_dir: Path
    log: logging.Logger
    # extra 字段会被 atexit / cleanup 写进 meta.json，让 backend daemon 自己
    # 持续更新（比如 child_pid 改了就改这里，cleanup 时一并落盘）。
    meta_extra: dict = field(default_factory=dict)


def init_lifecycle(
    alias: str,
    cwd: str,
    backend: str,
    log_filename: str = "daemon.log",
) -> DaemonContext:
    """准备日志、目录、初始 meta.json，返回上下文。

    backend 应是 `backends/` 下的目录名（"claude_code" / "hermes_acp" / …）。
    后续 `core/spawn.py` 按 meta.backend 字段选 daemon 模块。
    """
    if not ALIAS_RE.match(alias):
        raise ValueError(f"invalid alias: {alias!r}")

    sd = session_dir(alias)
    sd.mkdir(parents=True, exist_ok=True)

    log_path = sd / log_filename
    # NB: basicConfig 是进程级单次生效。daemon 是独立进程，每次启动新调用 OK。
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    log = logging.getLogger(f"daemon.{backend}")
    log.info("=" * 60)
    log.info("daemon[%s] starting alias=%s cwd=%s", backend, alias, cwd)

    ctx = DaemonContext(
        alias=alias,
        cwd=cwd,
        backend=backend,
        session_dir=sd,
        log=log,
        meta_extra={},
    )

    # 初始 meta 写盘——backend 之后用 update_meta() 增量补字段
    # daemon_create_time 给 sessions._reconcile_meta_liveness 做 PID 复用防护：
    # 如果 daemon 死了一周、OS 把这个 PID 复用给别的进程，比对 create_time
    # 能识破"假活"
    daemon_create_time: Optional[float] = None
    try:
        import psutil
        daemon_create_time = psutil.Process(os.getpid()).create_time()
    except Exception as e:
        log.warning("psutil create_time for daemon self failed: %s", e)
    write_meta(
        ctx,
        daemon_pid=os.getpid(),
        daemon_create_time=daemon_create_time,
        child_pid=None,
        created_at=_now_iso(),
    )
    return ctx


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── meta.json 增量更新 ────────────────────────────────────────────────────
def write_meta(ctx: DaemonContext, **fields) -> None:
    """更新 meta.json：合并新字段进现有 meta 并落盘。

    始终保证下列字段存在：alias / cwd / backend。其它字段由 backend 自己决定。
    """
    current = load_meta_for(ctx.alias) or {}
    current.update({
        "alias": ctx.alias,
        "cwd": ctx.cwd,
        "backend": ctx.backend,
    })
    current.update(fields)
    # 同时合并 ctx.meta_extra（让 backend 持续累加的字段不丢）
    for k, v in ctx.meta_extra.items():
        current.setdefault(k, v)
    save_meta_for(ctx.alias, current)


# ── spawned_pids.jsonl 追加 ──────────────────────────────────────────────
def record_spawned_child(ctx: DaemonContext, child_pid: int) -> None:
    """把 daemon 自己 spawn 的子进程 PID + create_time 记到
    `chat_sessions/<alias>/spawned_pids.jsonl`，让清理工具能识别。"""
    try:
        import psutil
        ct: Optional[float] = psutil.Process(child_pid).create_time()
    except Exception as e:
        ctx.log.warning("psutil create_time failed for child %s: %s", child_pid, e)
        ct = None

    rec = {
        "pid": child_pid,
        "create_time": ct,
        "spawned_at": _now_iso(),
        "daemon_pid": os.getpid(),
    }
    try:
        with (ctx.session_dir / "spawned_pids.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        ctx.log.warning("append spawned_pids.jsonl failed: %s", e)


# ── 退出清理 ──────────────────────────────────────────────────────────────
def install_cleanup(
    ctx: DaemonContext,
    on_exit: Optional[Callable[[], None]] = None,
) -> None:
    """装 atexit + SIGINT handler。

    on_exit：backend 自己提供的清理函数（杀子进程、关下游连接等）。本模块
    在它之后做"meta 标 offline"的通用动作，无论 on_exit 抛不抛异常。

    SIGINT 收到时：调 on_exit → 改 meta → sys.exit(0)。atexit 路径相同
    但不退出（Python 自然收尾）。两条路都用同一个内部清理函数，多次调用
    安全（带 once 标志）。
    """
    _done = {"ran": False}

    def _do_cleanup() -> None:
        if _done["ran"]:
            return
        _done["ran"] = True
        if on_exit is not None:
            try:
                on_exit()
            except Exception as e:
                ctx.log.warning("on_exit callback failed: %s", e)
        try:
            m = load_meta_for(ctx.alias) or {}
            m["daemon_pid"] = None
            m["child_pid"] = None
            m["last_exit_at"] = _now_iso()
            save_meta_for(ctx.alias, m)
        except Exception as e:
            ctx.log.warning("meta offline-mark failed: %s", e)

    atexit.register(_do_cleanup)

    def _sigint(_signum, _frame):
        ctx.log.info("SIGINT received, shutting down")
        print("\n[daemon] shutting down...", flush=True)
        _do_cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)
