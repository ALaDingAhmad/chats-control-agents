"""Spawn detached daemons and revive dead ones on demand.

Pure OS / subprocess work. Anything that needs to bring a backend daemon up
calls in here.

Two entry points:
  - spawn_daemon_detached(alias, cwd): start one, return PID or None.
  - ensure_daemon_alive(alias): async; idempotent. Used by the router when
    an inbound message arrives and the session's daemon may be dead.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime

from . import sessions as sx
from .paths import ROOT, loop_marker_fresh, loop_marker_path, outbox_path
from .pid_track import _pid_alive


log = logging.getLogger("core.spawn")

_HISTORICAL_CWD = str(ROOT.parent / "claude-code-account-switch")

# Total time we'll wait for ~/.claude/.chats-loop-active-<alias> after spawn.
READY_NOTIFY_TIMEOUT_SECS = 120.0


def _write_outbox_notice(alias: str, body: str) -> None:
    """Write a one-shot notice into outbox.txt for channel watchers."""
    stamp = datetime.now().strftime("%H:%M:%S")
    p = outbox_path(alias)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[{stamp}]\n{body}\n", encoding="utf-8")
        log.info("ready-notify[%s]: %s", alias, body[:80])
    except Exception as e:
        log.warning("ready-notify[%s]: write failed: %s", alias, e)


async def watch_ready(alias: str, daemon_pid: int) -> None:
    """Watch daemon liveness and readiness after spawn."""
    deadline = asyncio.get_event_loop().time() + READY_NOTIFY_TIMEOUT_SECS
    marker = loop_marker_path(alias)
    while asyncio.get_event_loop().time() < deadline:
        if marker.exists():
            _write_outbox_notice(alias, f"✅ 会话 {alias!r} 已就绪，可以继续发消息了。")
            return
        if not _pid_alive(daemon_pid):
            _write_outbox_notice(
                alias,
                f"⚠️ 会话 {alias!r} 启动后异常退出，见 chat_sessions/{alias}/daemon_stdout.log",
            )
            return
        await asyncio.sleep(1.0)
    _write_outbox_notice(
        alias,
        f"⚠️ 会话 {alias!r} 拉起超时（{int(READY_NOTIFY_TIMEOUT_SECS)}s 未就绪），"
        f"可能卡在 Claude TUI；见 chat_sessions/{alias}/daemon_stdout.log",
    )


_BACKEND_DAEMON_MODULES = {
    "claude_code": "chats_control_agents.backends.claude_code.daemon",
    "hermes_acp": "chats_control_agents.backends.hermes_acp.daemon",
}


def _resolve_daemon_module(alias: str) -> str:
    """Resolve daemon module from session meta; defaults to claude_code."""
    meta = sx.load_meta_for(alias) or {}
    backend = meta.get("backend") or "claude_code"
    mod = _BACKEND_DAEMON_MODULES.get(backend)
    if mod is None:
        log.warning("spawn[%s]: unknown backend %r, falling back to claude_code", alias, backend)
        return _BACKEND_DAEMON_MODULES["claude_code"]
    return mod


def spawn_daemon_detached(alias: str, cwd: str) -> int | None:
    """Spawn the daemon detached from this process."""
    stale = loop_marker_path(alias)
    if stale.exists():
        try:
            stale.unlink()
        except OSError:
            pass
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
        detached = 0x00000008
        new_process_group = 0x00000200
        no_window = 0x08000000
        kwargs["creationflags"] = detached | new_process_group | no_window
    else:
        kwargs["start_new_session"] = True

    daemon_module = _resolve_daemon_module(alias)
    try:
        proc = subprocess.Popen(["python", "-m", daemon_module, alias, cwd], **kwargs)
        log.info("spawn[%s]: backend-module=%s pid=%s cwd=%s", alias, daemon_module, proc.pid, cwd)
        return proc.pid
    except Exception as e:
        log.warning("spawn[%s]: failed: %s", alias, e)
        return None


async def ensure_daemon_alive(alias: str) -> bool:
    """If alias's daemon is dead, spawn a new one and wait until alive.

    Bridge-owned sessions (a user-opened claude window with cca-msg attached,
    no daemon): never stack a daemon on a live bridge — two consumers would
    race on the same inbox. Serviceable only if the chats-loop marker exists;
    a live bridge process alone just means the MCP server is attached, not
    that anyone is polling the inbox (docs/入站路由.md "终端 chats-loop 会话").
    """
    m = sx.load_meta_for(alias) or {}
    pid = m.get("daemon_pid")
    if pid and _pid_alive(pid):
        return True

    bridge_pid = m.get("bridge_pid")
    if bridge_pid and _pid_alive(bridge_pid):
        return loop_marker_fresh(alias)

    cwd = m.get("cwd") or _HISTORICAL_CWD
    log.info("ensure[%s]: daemon pid=%s dead, respawning at cwd=%s", alias, pid, cwd)
    spawned_pid = spawn_daemon_detached(alias, cwd)
    if not spawned_pid:
        log.warning("ensure[%s]: spawn failed", alias)
        return False

    for _ in range(20):
        if _pid_alive(spawned_pid):
            log.info("ensure[%s]: daemon pid=%s alive", alias, spawned_pid)
            asyncio.create_task(watch_ready(alias, spawned_pid))
            return True
        await asyncio.sleep(0.1)
    log.warning("ensure[%s]: pid=%s never went live", alias, spawned_pid)
    return False
