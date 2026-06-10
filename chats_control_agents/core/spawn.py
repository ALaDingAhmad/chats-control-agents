"""Spawn detached daemons and revive dead ones on demand.

Pure OS / subprocess work — no web framework, no channel protocol. Anything
that needs to bring a backend daemon up calls in here.

Two entry points:
  - spawn_daemon_detached(alias, cwd) — start one, return PID or None.
  - ensure_daemon_alive(alias) — async; idempotent. Used by the router when
    an inbound message arrives and the session's daemon may be dead.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from . import sessions as sx
from .paths import ROOT, outbox_path
from .pid_track import _pid_alive


log = logging.getLogger("core.spawn")

_HISTORICAL_CWD = str(ROOT.parent / "claude-code-account-switch")

# Total time we'll wait for `~/.claude/.chats-loop-active-<alias>` to appear
# after we spawn a daemon. Covers: TUI cold start, trust-folder dialog,
# /chats-loop trigger, skill init, first wait_for_message. 60s is generous
# but not absurd; the user pays nothing for a successful spawn since the
# notify fires as soon as the marker shows up.
READY_NOTIFY_TIMEOUT_SECS = 60.0
_MARKER_DIR = Path.home() / ".claude"


def _marker_path(alias: str) -> Path:
    return _MARKER_DIR / f".chats-loop-active-{alias}"


def _write_outbox_notice(alias: str, body: str) -> None:
    """Drop a one-shot notice into the alias's outbox so every channel's
    outbox_watcher forwards it to the user. Mirrors the daemon's notice
    format so the watcher's dedup fingerprint behaves consistently."""
    stamp = datetime.now().strftime("%H:%M:%S")
    p = outbox_path(alias)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[{stamp}]\n{body}\n", encoding="utf-8")
        log.info("ready-notify[%s]: %s", alias, body[:80])
    except Exception as e:
        log.warning("ready-notify[%s]: write failed: %s", alias, e)


async def watch_ready(alias: str, daemon_pid: int) -> None:
    """Background task: wait for the chats-loop skill to activate, then
    write a user-facing notice via outbox. Started by anyone who just
    spawned a daemon for `alias`.

    Outcomes (always exactly one):
      • marker file appears   → "✅ 已就绪"
      • daemon process dies   → "⚠️ 启动后异常退出"
      • timeout               → "⚠️ 拉起超时"
    """
    marker = _marker_path(alias)
    deadline = asyncio.get_event_loop().time() + READY_NOTIFY_TIMEOUT_SECS
    while asyncio.get_event_loop().time() < deadline:
        if marker.exists():
            _write_outbox_notice(
                alias, f"✅ 会话 {alias!r} 已就绪，发消息试试"
            )
            return
        if not _pid_alive(daemon_pid):
            _write_outbox_notice(
                alias,
                f"⚠️ 会话 {alias!r} 启动后异常退出，"
                f"看 chat_sessions/{alias}/daemon.log",
            )
            return
        await asyncio.sleep(0.5)
    _write_outbox_notice(
        alias,
        f"⚠️ 会话 {alias!r} 拉起超时（{int(READY_NOTIFY_TIMEOUT_SECS)}s 未就绪），"
        f"可能 child claude 卡在某个弹窗。看 chat_sessions/{alias}/pty.log",
    )


# backend 名 → daemon 模块路径。新加 backend 时在这里登记一条。
# 不在这里登记的 backend 起不来——这是有意的最小注册表，避免 import 时副作用。
_BACKEND_DAEMON_MODULES = {
    "claude_code": "chats_control_agents.backends.claude_code.daemon",
    "hermes_acp":  "chats_control_agents.backends.hermes_acp.daemon",
}


def _resolve_daemon_module(alias: str) -> str:
    """根据 alias 的 meta.json 选 daemon 模块。缺省 claude_code 向后兼容。"""
    meta = sx.load_meta_for(alias) or {}
    backend = meta.get("backend") or "claude_code"
    mod = _BACKEND_DAEMON_MODULES.get(backend)
    if mod is None:
        log.warning("spawn[%s]: unknown backend %r, falling back to claude_code", alias, backend)
        return _BACKEND_DAEMON_MODULES["claude_code"]
    return mod


def spawn_daemon_detached(alias: str, cwd: str) -> int | None:
    """Spawn the daemon detached from this process. Returns PID or None on failure.

    Windows: DETACHED + CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW so the
    daemon survives web_server restart and doesn't pop a console window.
    Unix: start_new_session to detach from the caller's process group.

    具体起哪个 backend 的 daemon 由 `meta.json.backend` 字段决定（缺省
    `claude_code` 向后兼容）。
    """
    log_path = sx.session_dir(alias) / "daemon_stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(log_path, "a", encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("spawn[%s]: could not open log: %s", alias, e)
        return None
    kwargs: dict = {
        "stdout": f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(ROOT),
        "close_fds": True,
    }
    if os.name == "nt":
        DETACHED = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    daemon_module = _resolve_daemon_module(alias)
    try:
        proc = subprocess.Popen(
            ["python", "-m", daemon_module, alias, cwd],
            **kwargs,
        )
        log.info("spawn[%s]: backend-module=%s pid=%s cwd=%s", alias, daemon_module, proc.pid, cwd)
        return proc.pid
    except Exception as e:
        log.warning("spawn[%s]: failed: %s", alias, e)
        return None


async def ensure_daemon_alive(alias: str) -> bool:
    """If alias's daemon is dead, spawn a new one and wait until ready.

    Returns True if a daemon is alive (already was, or successfully revived).
    Returns False if spawn failed or the process never went live — caller
    should surface an "agent failed to come up" message to the user.
    """
    m = sx.load_meta_for(alias) or {}
    pid = m.get("daemon_pid")
    if pid and _pid_alive(pid):
        return True

    cwd = m.get("cwd") or _HISTORICAL_CWD
    log.info("ensure[%s]: daemon pid=%s dead, respawning at cwd=%s", alias, pid, cwd)
    spawned_pid = spawn_daemon_detached(alias, cwd)
    if not spawned_pid:
        log.warning("ensure[%s]: spawn failed", alias)
        return False

    # Ready = daemon process is alive. We used to additionally wait for the
    # skill-activated marker in daemon.log, but that's flaky — Claude can take
    # 20-40s to do TUI startup → /chats-loop slash → env lookup → relay_init
    # → first wait_for_message, and the harness sometimes never prints the
    # exact marker string we looked for. Writing to inbox is safe even if
    # skill is still initializing: mcp_bridge.py polls the inbox file at 0.5s
    # cadence inside wait_for_message, so the message will be picked up the
    # moment the loop starts.
    for _ in range(20):  # give the OS a moment to schedule the new daemon process
        if _pid_alive(spawned_pid):
            log.info("ensure[%s]: daemon pid=%s alive", alias, spawned_pid)
            # Fire-and-forget readiness watcher. Notifies user (via outbox)
            # once chats-loop skill activates, or surfaces a failure if it
            # never does. See docs/ROUTING.md "就绪通知".
            asyncio.create_task(watch_ready(alias, spawned_pid))
            return True
        await asyncio.sleep(0.1)
    log.warning("ensure[%s]: pid=%s never went live", alias, spawned_pid)
    return False
