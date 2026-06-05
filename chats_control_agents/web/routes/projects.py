"""Workspace + projects routes: list projects, get/set workspace_roots."""
from __future__ import annotations

import logging

from starlette.responses import JSONResponse

from ...core import config as cfg
from ...core import projects as proj


log = logging.getLogger("web.projects")


async def list_projects_route(request):
    return JSONResponse({
        "workspace_roots": [str(r) for r in cfg.get_workspace_roots()],
        "projects": proj.list_projects(),
    })


async def get_config_route(request):
    config = cfg.load_config()
    return JSONResponse({
        "workspace_roots": config.get("workspace_roots") or [],
        "existing": [str(r) for r in cfg.get_workspace_roots()],
    })


async def update_workspace_route(request):
    """POST {"workspace_roots": [...]} — validate, save, return updated config.

    Paths are normalized but not required to exist on disk; the frontend
    visually flags missing entries based on config.existing.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "reason": "invalid JSON"}, status_code=400)
    raw = body.get("workspace_roots")
    if not isinstance(raw, list):
        return JSONResponse({"ok": False, "reason": "workspace_roots must be a list"}, status_code=400)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        s = s.replace("\\", "/").rstrip("/")
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(s)
    config = cfg.load_config()
    config["workspace_roots"] = cleaned
    try:
        cfg.save_config(config)
    except Exception as e:
        log.warning("save_config failed: %s", e)
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)
    log.info("config: workspace_roots updated → %d entries", len(cleaned))
    return JSONResponse({
        "ok": True,
        "workspace_roots": cleaned,
        "existing": [str(r) for r in cfg.get_workspace_roots()],
    })
