"""Sessions route — list all sessions and current selection."""
from __future__ import annotations

from starlette.responses import JSONResponse

from ...core import sessions as sx


async def list_sessions_route(request):
    return JSONResponse({
        "sessions": sx.list_sessions(),
        "current": sx.get_current(),
    })
