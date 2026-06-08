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

_HISTORICAL_CWD = str(ROOT.parent / "claude-code-account-switch")
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

    cwd = m.get("cwd") or _HISTORICAL_CWD
    log.info("ensure[%s]: daemon pid=%s dead, respawning at cwd=%s", alias, pid, cwd)
    spawned_pid = _spawn_daemon_detached(alias, cwd)
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
    # moment the loop starts. The user pays at most a 20-30s reply latency
    # on cold-start, which is the correct trade vs. the previous behavior of
    # silently dropping the message after 15s.
    for _ in range(20):  # give the OS a moment to schedule the new daemon process
        if _pid_alive(spawned_pid):
            log.info("ensure[%s]: daemon pid=%s spawned and alive", alias, spawned_pid)
            return True
        await asyncio.sleep(0.1)
    log.warning("ensure[%s]: spawned pid=%s never went live", alias, spawned_pid)
    return False


# ── Dashboard-driven new session ─────────────────────────────────────────
async def spawn_new_session(mode: str, project_cwd: str | None = None) -> dict:
    """Create a fresh session for the user from the dashboard.

    mode='chat'    → cwd = home, alias = <home basename>-<MMDD-HHMM>
    mode='project' → cwd = project_cwd, alias = <basename>-<MMDD-HHMM>

    Spawns a detached daemon, waits until ready, marks the new alias as
    current, and returns {ok, alias, error?}.
    """
    from pathlib import Path
    from ..core.sessions import make_alias_for_cwd, set_current
    if mode == "chat":
        cwd = str(Path.home())
    elif mode == "project":
        if not project_cwd:
            return {"ok": False, "error": "project mode requires project_cwd"}
        if not Path(project_cwd).is_dir():
            return {"ok": False, "error": f"not a directory: {project_cwd}"}
        cwd = project_cwd
    else:
        return {"ok": False, "error": f"unknown mode: {mode!r}"}

    alias = make_alias_for_cwd(cwd)
    log.info("spawn_new_session[%s]: mode=%s cwd=%s", alias, mode, cwd)
    pid = _spawn_daemon_detached(alias, cwd)
    if not pid:
        return {"ok": False, "error": "daemon spawn failed", "alias": alias}

    # Wait for ready, same way ensure_daemon_alive does.
    log_path = sx.session_dir(alias) / "daemon.log"
    deadline = time.time() + _READY_TIMEOUT_SECS
    baseline = log_path.stat().st_size if log_path.exists() else 0
    while time.time() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_SECS)
        if not log_path.exists():
            continue
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(baseline)
                fresh = f.read()
        except Exception:
            continue
        if "skill activated" in fresh:
            try:
                set_current(alias)
            except Exception as e:
                log.warning("spawn_new_session[%s]: set_current failed: %s", alias, e)
            log.info("spawn_new_session[%s]: ready", alias)
            return {"ok": True, "alias": alias}

    log.warning("spawn_new_session[%s]: ready timeout", alias)
    return {"ok": False, "error": "daemon ready timeout", "alias": alias}
