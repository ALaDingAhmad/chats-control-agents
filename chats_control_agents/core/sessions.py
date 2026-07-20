"""Session registry: alias notion, current selection, liveness listing.

A *session* is a directory `chat_sessions/<alias>/` holding:
  inbox.txt          — last message a channel wrote for the backend
  outbox.txt         — last reply a backend wrote for the channel
  history.json       — full chronological message log (for UI display)
  meta.json          — {alias, cwd, daemon_pid, child_pid, created_at, …}
  spawned_pids.jsonl — append-only daemon-spawned child PIDs
  daemon.log, pty.log, daemon_stdout.log — daemon runtime logs

No command processing here — see commands.py.
"""
from __future__ import annotations

import json
from typing import Optional

from datetime import datetime
from pathlib import Path
import re as _re

from .paths import (
    ALIAS_RE,
    CURRENT_FILE,
    DEFAULT_BACKEND_FILE,
    LEGACY_DEFAULT_ALIAS,
    SESSIONS_ROOT,
    history_path,
    inbox_path,
    loop_marker_fresh,
    meta_path,
    outbox_path,
    session_dir,
)
from .pid_track import _pid_alive


# 已知 backend 集合（跟 core.spawn._BACKEND_DAEMON_MODULES 一致）
KNOWN_BACKENDS = ("claude_code", "hermes_acp", "claude_channel")
_BACKEND_NAME_RE = _re.compile(r"^[a-z][a-z0-9_]{0,31}$")


# ── Current selection (global, single-user) ──────────────────────────────
def get_current() -> Optional[str]:
    """Returns the currently selected alias, or None if nothing is selected.

    No default fallback: post-migration, every session is named explicitly
    (project name + time). The web layer prompts the user to create one if
    no session exists yet.
    """
    if CURRENT_FILE.exists():
        try:
            cur = CURRENT_FILE.read_text(encoding="utf-8").strip()
            if cur and ALIAS_RE.match(cur):
                return cur
        except Exception:
            pass
    return None


# ── Alias generation: <project>-<MMDD-HHMM>, sanitized ───────────────────
_ALIAS_BAD = _re.compile(r"[^a-zA-Z0-9_\-一-鿿]+")


def _sanitize_alias_part(s: str) -> str:
    """Replace any char not in ALIAS_RE with '-', collapse runs of '-'."""
    cleaned = _ALIAS_BAD.sub("-", s).strip("-")
    cleaned = _re.sub(r"-+", "-", cleaned)
    return cleaned or "x"


def make_alias_for_cwd(cwd: str | Path, when: datetime | None = None) -> str:
    """Build an alias like 'agent-bridge-0605-1130' from a cwd.

    - project name = basename(cwd)
    - timestamp = MMDD-HHMM (default now)
    - sanitized to fit ALIAS_RE, truncated to 32 chars (the regex hard limit).
    """
    when = when or datetime.now()
    stem = _sanitize_alias_part(Path(cwd).name)
    stamp = when.strftime("%m%d-%H%M")
    full = f"{stem}-{stamp}"
    if len(full) <= 32:
        return full
    # Need to trim the project part so timestamp survives.
    keep = 32 - len(stamp) - 1
    return f"{stem[:keep]}-{stamp}"


def set_current(alias: str) -> None:
    if not ALIAS_RE.match(alias):
        raise ValueError(f"invalid alias: {alias!r}")
    session_dir(alias).mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(alias, encoding="utf-8")
    meta = load_meta_for(alias)
    if isinstance(meta, dict):
        meta = dict(meta)
        meta["selected_at"] = datetime.now().isoformat(timespec="seconds")
        save_meta_for(alias, meta)


# ── Default backend (sticky per install, read by /proj when creating sessions) ──
def get_default_backend() -> str:
    """读默认 backend；文件不存在或无效时回 claude_code。"""
    if DEFAULT_BACKEND_FILE.exists():
        try:
            val = DEFAULT_BACKEND_FILE.read_text(encoding="utf-8").strip()
            if val in KNOWN_BACKENDS:
                return val
        except Exception:
            pass
    return "claude_code"


def set_default_backend(name: str) -> None:
    """切默认 backend。校验过 KNOWN_BACKENDS，外部仍需要兜底 ValueError。"""
    if not _BACKEND_NAME_RE.match(name) or name not in KNOWN_BACKENDS:
        raise ValueError(f"unknown backend: {name!r}")
    DEFAULT_BACKEND_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_BACKEND_FILE.write_text(name, encoding="utf-8")


# ── Meta access ──────────────────────────────────────────────────────────
def load_meta_for(alias: str) -> Optional[dict]:
    p = meta_path(alias)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_meta_for(alias: str, meta: dict) -> None:
    p = meta_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ── Create a new session directory + initial meta ────────────────────────
def create_session_dir(
    alias: str,
    cwd: str,
    backend: str = "claude_code",
) -> None:
    """建会话目录并写初始 meta.json。

    所有"建会话"入口（命令行 /proj、web dashboard 新建按钮、未来的 CLI）
    都该走这条路，避免散在多处的字段手写漏掉 `backend`——`backend` 字段
    决定 `core.spawn._resolve_daemon_module` 起哪个 daemon。

    meta 里 daemon_pid/child_pid 留 None，由 daemon 自己启动后用
    `daemon_lifecycle.write_meta` 补。
    """
    if not ALIAS_RE.match(alias):
        raise ValueError(f"invalid alias: {alias!r}")
    sd = session_dir(alias)
    sd.mkdir(parents=True, exist_ok=True)
    save_meta_for(alias, {
        "alias": alias,
        "cwd": cwd,
        "backend": backend,
        "daemon_pid": None,
        "child_pid": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })


# ── Session listing ──────────────────────────────────────────────────────
def _reconcile_meta_liveness(alias: str, meta: dict) -> tuple[dict, bool]:
    """Lazy-fix：meta 字面声称在线（daemon_pid 非 null）但 PID 已不活时，
    返回修正过的 meta 副本（daemon_pid / child_pid 清 None、写
    `last_exit_at="(detected_dead)"`）+ True。

    无需修正时返回原 meta + False。

    daemon 自己注册了 atexit 钩子写这些字段，但被 `taskkill /F`、断电、
    OOM、Python 解释器崩等情况绕过时，meta 会留死 PID，把"在线"假象
    带到 dashboard 和命令行 /list 上。本函数负责把字面状态拽回真实。

    PID 复用防护：daemon 启动时通过 `init_lifecycle` 写
    `daemon_create_time`（psutil.Process.create_time()）；本函数判活时
    除了 `_pid_alive` 还会比对 create_time。如果 PID 还在但是 create_time
    对不上，说明那是 OS 复用后的另一个进程（比如 daemon 死了一周、PID
    被某 Edge 标签拿去用了）——视同已死，回写清字段。

    向后兼容：老 meta 没有 `daemon_create_time` 字段时退化到只用
    `_pid_alive`（跟改动前同行为）。
    """
    changed = False
    fixed = dict(meta)

    daemon_pid = meta.get("daemon_pid")
    if daemon_pid:
        if not _pid_alive(daemon_pid):
            fixed["daemon_pid"] = None
            fixed["child_pid"] = None
            fixed.setdefault("last_exit_at", "(detected_dead)")
            changed = True
        else:
            logged_ct = meta.get("daemon_create_time")
            if logged_ct is not None:
                try:
                    import psutil
                    actual_ct = psutil.Process(daemon_pid).create_time()
                    if abs(actual_ct - logged_ct) >= 1.0:
                        fixed["daemon_pid"] = None
                        fixed["child_pid"] = None
                        fixed.setdefault("last_exit_at", "(detected_dead_pid_recycled)")
                        changed = True
                except Exception:
                    pass

    bridge_pid = meta.get("bridge_pid")
    if bridge_pid:
        if not _pid_alive(bridge_pid):
            fixed["bridge_pid"] = None
            changed = True
        else:
            logged_ct = meta.get("bridge_create_time")
            if logged_ct is not None:
                try:
                    import psutil
                    actual_ct = psutil.Process(bridge_pid).create_time()
                    if abs(actual_ct - logged_ct) >= 1.0:
                        fixed["bridge_pid"] = None
                        changed = True
                except Exception:
                    pass

    return fixed, changed


def list_sessions() -> list[dict]:
    """Scan chat_sessions/ for all aliases. Sorted: online first, then by
    recency descending.

    顺带做 lazy-fix：扫到 meta 字面声称在线但 PID 不活的会话时，
    回写 meta 把字段清成 null（见 `_reconcile_meta_liveness`）。"""
    cur = get_current()
    out: list[dict] = []
    for entry in SESSIONS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        alias = entry.name
        if not ALIAS_RE.match(alias):
            continue
        m = load_meta_for(alias) or {}
        m, changed = _reconcile_meta_liveness(alias, m)
        if changed:
            try:
                save_meta_for(alias, m)
            except Exception:
                pass  # 写盘失败不阻塞列表渲染——下次扫描再尝试
        daemon_pid = m.get("daemon_pid")
        bridge_pid = m.get("bridge_pid")
        # bridge 活只说明 MCP 挂着；真在收件要看 marker 新鲜度（docs/入站路由.md）
        online = (bool(daemon_pid) and _pid_alive(daemon_pid)) or (
            bool(bridge_pid) and _pid_alive(bridge_pid) and loop_marker_fresh(alias)
        )
        out.append({
            "alias": alias,
            "cwd": m.get("cwd", ""),
            "online": online,
            "daemon_pid": daemon_pid if online else None,
            "created_at": m.get("created_at"),
            "last_exit_at": m.get("last_exit_at"),
            "last_active": _last_active(alias),
            "current": alias == cur,
            "backend": m.get("backend", "claude_code"),
        })
    out.sort(key=lambda s: (not s["online"], -(s["last_active"] or 0)))
    return out


def _last_active(alias: str) -> Optional[float]:
    """Approximate last activity = newest of inbox/outbox/history mtime."""
    latest = 0.0
    for fn in (inbox_path, outbox_path, history_path):
        p = fn(alias)
        if p.exists():
            try:
                latest = max(latest, p.stat().st_mtime)
            except Exception:
                pass
    return latest or None


# ── Migration: pull legacy single-session files into chat_sessions/default/ ──
def migrate_legacy_if_present() -> None:
    """One-shot: if old chat_inbox.txt etc. exist at project root, move them
    into chat_sessions/default/. Safe to call on every startup."""
    from .paths import ROOT
    sd = session_dir(LEGACY_DEFAULT_ALIAS)
    sd.mkdir(parents=True, exist_ok=True)
    moves = [
        ("chat_inbox.txt", "inbox.txt"),
        ("chat_outbox.txt", "outbox.txt"),
        ("chat_history.json", "history.json"),
    ]
    for old_name, new_name in moves:
        old = ROOT / old_name
        new = sd / new_name
        if old.exists() and not new.exists():
            try:
                old.rename(new)
            except Exception:
                pass
        elif old.exists() and new.exists():
            try:
                old.rename(ROOT / (old_name + ".legacy"))
            except Exception:
                pass
