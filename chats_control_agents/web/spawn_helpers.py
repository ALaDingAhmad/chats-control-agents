"""Web-layer spawn helpers.

The OS-level "spawn detached daemon" and "ensure daemon alive" primitives
moved to core/spawn.py; this module re-exports them for backward compat
and keeps the dashboard-driven `spawn_new_session` (which is web-flow
specific — it watches daemon.log for the skill-activation marker).
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..core import sessions as sx
from ..core.spawn import ensure_daemon_alive, spawn_daemon_detached  # noqa: F401 (re-export)


log = logging.getLogger("web.spawn")

_READY_TIMEOUT_SECS = 15.0
_POLL_INTERVAL_SECS = 0.5


# Back-compat alias for callers that still import the leading-underscore name.
_spawn_daemon_detached = spawn_daemon_detached


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
    pid = spawn_daemon_detached(alias, cwd)
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
