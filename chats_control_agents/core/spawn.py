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

from . import sessions as sx
from .paths import ROOT
from .pid_track import _pid_alive


log = logging.getLogger("core.spawn")

_HISTORICAL_CWD = str(ROOT.parent / "claude-code-account-switch")


def spawn_daemon_detached(alias: str, cwd: str) -> int | None:
    """Spawn the daemon detached from this process. Returns PID or None on failure.

    Windows: DETACHED + CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW so the
    daemon survives web_server restart and doesn't pop a console window.
    Unix: start_new_session to detach from the caller's process group.
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
    try:
        proc = subprocess.Popen(
            ["python", "-m", "chats_control_agents.backends.claude_code.daemon", alias, cwd],
            **kwargs,
        )
        log.info("spawn[%s]: pid=%s cwd=%s", alias, proc.pid, cwd)
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
            return True
        await asyncio.sleep(0.1)
    log.warning("ensure[%s]: pid=%s never went live", alias, spawned_pid)
    return False
