"""On-demand daemon revival: inbound message paths call ensure_daemon_alive()
before writing inbox so a long-idle session whose daemon died gets respawned
right when a user tries to talk to it.

Why not a periodic watchdog: a dead daemon with no inbound is harmless; the
moment it matters is when a message arrives. Tying revival to message arrival
is the most precise signal and avoids periodic spawn churn.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..core import sessions as sx
from ..core.paths import ROOT
from ..core.pid_track import _pid_alive
from .autospawn import _spawn_daemon_detached


log = logging.getLogger("web.spawn")

_DEFAULT_CWD = str(ROOT.parent / "claude-code-account-switch")
_READY_TIMEOUT_SECS = 15.0
_POLL_INTERVAL_SECS = 0.5


async def ensure_daemon_alive(alias: str) -> bool:
    """If alias's daemon is dead, spawn a new one and wait until ready.

    Returns True if a daemon is alive (already was, or successfully revived).
    Returns False if spawn failed or ready timed out — caller should surface
    a "agent failed to come up" message to the user.
    """
    m = sx.load_meta_for(alias) or {}
    pid = m.get("daemon_pid")
    if pid and _pid_alive(pid):
        return True

    cwd = m.get("cwd") or _DEFAULT_CWD
    log.info("ensure[%s]: daemon pid=%s dead, respawning at cwd=%s", alias, pid, cwd)
    spawned_pid = _spawn_daemon_detached(alias, cwd)
    if not spawned_pid:
        log.warning("ensure[%s]: spawn failed", alias)
        return False

    # Ready signal: daemon writes "skill activated" to daemon.log once the
    # child claude has loaded web-relay and is sitting in the message loop.
    # meta.json gets the new pid earlier (right after spawning child claude),
    # but child isn't actually ready to read inbox until skill activates —
    # checking the log is more accurate than checking meta.
    log_path = sx.session_dir(alias) / "daemon.log"
    deadline = time.time() + _READY_TIMEOUT_SECS
    baseline_size = log_path.stat().st_size if log_path.exists() else 0
    while time.time() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_SECS)
        if not log_path.exists():
            continue
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(baseline_size)
                fresh = f.read()
        except Exception:
            continue
        if "skill activated" in fresh:
            log.info("ensure[%s]: daemon ready (skill activated)", alias)
            return True

    log.warning("ensure[%s]: timeout waiting for skill activation", alias)
    return False
