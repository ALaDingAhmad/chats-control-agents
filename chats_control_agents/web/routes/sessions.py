"""Sessions route — list all sessions and current selection."""
from __future__ import annotations

import logging

from starlette.responses import JSONResponse

from ...core import sessions as sx
from ...core.pid_track import _pid_alive


log = logging.getLogger("web.sessions")


async def list_sessions_route(request):
    return JSONResponse({
        "sessions": sx.list_sessions(),
        "current": sx.get_current(),
    })


async def set_current_route(request):
    """POST /session/use {alias} — set the currently-selected alias.
    Used by settings.html sessions tab."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    alias = (body.get("alias") or "").strip()
    if not alias:
        return JSONResponse({"ok": False, "error": "alias required"})
    try:
        sx.set_current(alias)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    log.info("current alias set to %s", alias)
    return JSONResponse({"ok": True, "alias": alias})


async def end_daemon_route(request):
    """POST /session/end {alias} — kill the daemon for an alias if alive.
    The session dir is kept, history preserved, just the live process goes."""
    import os
    import signal
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    alias = (body.get("alias") or "").strip()
    if not alias:
        return JSONResponse({"ok": False, "error": "alias required"})
    m = sx.load_meta_for(alias) or {}
    pid = m.get("daemon_pid")
    if not pid or not _pid_alive(pid):
        return JSONResponse({"ok": True, "noop": True, "alias": alias})
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"kill failed: {e}"})
    log.info("ended daemon pid=%s for alias=%s", pid, alias)
    return JSONResponse({"ok": True, "alias": alias, "pid": pid})
