"""hermes_acp daemon: 1 alias = 1 daemon = 1 `hermes acp` 子进程 = 1 ACP session。

daemon **在消息路径上**（"路径内"型 backend，对比 claude_code 的"路径外"）：

  router → inbox.txt
              ↑ poll(0.5s)
       ┌──────┴──────┐
       │ this daemon │ ── stdio JSON-RPC ↔ hermes acp subprocess
       └──────┬──────┘
              ↓ write outbox.txt
                                                 → web /poll, weixin watcher

启动流程（init_lifecycle 起骨架，acp_client 起协议）：
  1. parse_cli_args → alias + cwd
  2. init_lifecycle(backend="hermes_acp") → 日志、meta、session_dir
  3. AcpClient.start() → spawn `hermes acp` 子进程
  4. initialize → new_session(cwd) → 拿 sessionId（首次 60-80s）
  5. install_cleanup(on_exit=acp.stop)
  6. 写 ready marker（`~/.claude/.chats-loop-active-<alias>`），让 watch_ready
     给用户发"会话已就绪"通知
  7. 进入主循环：poll inbox.txt，新内容 → AcpClient.prompt → 拿 turn 结果 → 写 outbox.txt

设计说明见 `docs/BACKEND-DESIGN.md` 和 `docs/HERMES-ACP-SPIKE.md`。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from chats_control_agents.core import daemon_lifecycle as lc
from chats_control_agents.core.paths import inbox_path, outbox_path

from .acp_client import AcpClient


# inbox 轮询间隔 —— 跟 claude_code mcp_bridge 一致
POLL_INTERVAL_SECS = 0.5

# ready marker：跟 claude_code 共用同一组目录约定，这样 web spawn.watch_ready
# 不用为新 backend 改任何代码就能识别"就绪"
_MARKER_DIR = Path.home() / ".claude"

# CLI: python -m chats_control_agents.backends.hermes_acp.daemon [<alias>] [<cwd>]
ALIAS, CWD_ARG = lc.parse_cli_args(default_cwd=Path.home())


def _marker_path(alias: str) -> Path:
    return _MARKER_DIR / f".chats-loop-active-{alias}"


def _write_outbox(alias: str, text: str) -> None:
    """跟 claude_code mcp_bridge 一致的 outbox 格式：覆写 `[HH:MM:SS]\\n<reply>\\n`。"""
    stamp = datetime.now().strftime("%H:%M:%S")
    p = outbox_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"[{stamp}]\n{text}\n", encoding="utf-8")


def _drain_inbox(alias: str, last_mtime: float) -> tuple[str | None, float]:
    """读 inbox.txt 如果有"新"内容（mtime 比 last_mtime 大），返回 (text, new_mtime)。
    没新内容返回 (None, last_mtime)。
    """
    p = inbox_path(alias)
    if not p.exists():
        return None, last_mtime
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None, last_mtime
    if mt <= last_mtime:
        return None, last_mtime
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception:
        return None, mt
    if not text:
        return None, mt
    return text, mt


async def _main_async(log: logging.Logger, ctx: lc.DaemonContext) -> int:
    stderr_log = str(ctx.session_dir / "hermes_stderr.log")
    acp = AcpClient(stderr_log_path=stderr_log)

    # 启动 hermes acp 子进程
    try:
        child_pid = await acp.start()
    except Exception as e:
        log.exception("AcpClient.start failed: %s", e)
        return 2
    log.info("hermes acp pid=%s, stderr→%s", child_pid, stderr_log)
    print(f"[daemon] hermes acp pid={child_pid}")
    lc.write_meta(ctx, child_pid=child_pid)
    lc.record_spawned_child(ctx, child_pid)

    # 退出清理：先停 acp，再让 lifecycle 标 meta offline。停 acp 在 async
    # 上下文里需要安排，atexit 是同步钩子——用 run_until_complete 兜底。
    def _on_exit() -> None:
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(acp.stop())
            finally:
                loop.close()
        except Exception as e:
            log.warning("acp.stop in atexit failed: %s", e)
    lc.install_cleanup(ctx, on_exit=_on_exit)

    # initialize
    try:
        init = await acp.initialize()
        log.info("initialize ok: agent=%s caps=%s",
                 init.get("agentInfo"), init.get("agentCapabilities"))
    except Exception as e:
        log.exception("initialize failed: %s", e)
        return 3

    # new_session（首次 60-80s）
    log.info("session/new cwd=%s (首次可能 60-80s)", ctx.cwd)
    print("[daemon] session/new …（首次会比较慢）")
    try:
        session_id = await acp.new_session(ctx.cwd)
    except Exception as e:
        log.exception("session/new failed: %s", e)
        return 4
    log.info("session_id=%s", session_id)
    lc.write_meta(ctx, session_id=session_id)
    print(f"[daemon] session_id={session_id}")

    # 写 ready marker，让 web/spawn.watch_ready 给用户发就绪通知
    try:
        _MARKER_DIR.mkdir(parents=True, exist_ok=True)
        _marker_path(ALIAS).write_text(
            f"{os.getpid()}\n{datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("ready marker write failed: %s", e)

    # inbox 轮询主循环。startup 时不把 inbox 里残留的旧消息当新消息——以
    # 当前 mtime 为 baseline，只处理之后的写入。
    p = inbox_path(ALIAS)
    last_mtime = p.stat().st_mtime if p.exists() else 0.0
    log.info("entering inbox poll loop, baseline mtime=%s", last_mtime)
    print("[daemon] ready, polling inbox…")

    try:
        while acp.is_alive():
            await asyncio.sleep(POLL_INTERVAL_SECS)
            text, new_mtime = _drain_inbox(ALIAS, last_mtime)
            if text is None:
                continue
            last_mtime = new_mtime
            log.info("inbox new msg (%d chars): %r", len(text), text[:120])

            try:
                turn = await acp.prompt(session_id, text)
            except Exception as e:
                log.exception("prompt failed: %s", e)
                _write_outbox(ALIAS, f"⚠️ hermes 处理失败：{type(e).__name__}: {e}")
                continue

            log.info(
                "turn done stop_reason=%s reply_len=%d thoughts_len=%d tools=%d usage=%s",
                turn.stop_reason, len(turn.text), len(turn.thoughts), turn.tool_calls, turn.usage,
            )
            if turn.text:
                _write_outbox(ALIAS, turn.text)
            else:
                # 模型没给最终回复（reasoning 模型把答案全塞 thoughts 里这种）
                # 写一个能让用户知道"我处理了但没文字输出"的提示。日志里
                # thoughts 是有的，可以人工 debug。
                _write_outbox(
                    ALIAS,
                    f"⚠️ hermes 本轮没产出最终回复（stop_reason={turn.stop_reason}）。"
                    "看 chat_sessions/<alias>/daemon.log 的 thoughts 段调试。"
                )
    except asyncio.CancelledError:
        log.info("main loop cancelled")
    except Exception:
        log.exception("main loop crashed")
        return 5

    log.info("acp child no longer alive, exiting")
    return 0


def main() -> int:
    # 决定 spawn cwd（CLI > meta 历史 > $HOME）
    spawn_cwd = lc.resolve_spawn_cwd(CWD_ARG, ALIAS, backend_default=Path.home())

    # 装日志 / session_dir / 初始 meta
    ctx = lc.init_lifecycle(alias=ALIAS, cwd=spawn_cwd, backend="hermes_acp")
    log = ctx.log
    print(f"[daemon] alias: {ALIAS}")
    print(f"[daemon] session dir: {ctx.session_dir}")
    print(f"[daemon] cwd: {spawn_cwd}")

    try:
        return asyncio.run(_main_async(log, ctx))
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt, exiting")
        return 0


if __name__ == "__main__":
    sys.exit(main())
