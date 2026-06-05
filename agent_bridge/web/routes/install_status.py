"""Install status + actions for the Claude Code settings tab.

We don't import install/install.py directly because it prints to stdout —
we'd have to either monkey-patch print or refactor the installer to return
structured data. Either is overkill for a settings UI. Instead we shell out
to `python install/install.py --dry-run` (or the real flags) and surface
its output.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from starlette.responses import JSONResponse

from ...core.paths import ROOT


log = logging.getLogger("web.install")

INSTALLER = ROOT / "install" / "install.py"


async def _run_installer(*flags: str) -> tuple[int, str]:
    """Run install.py with given flags, capture combined stdout/stderr."""
    if not INSTALLER.exists():
        return 2, f"installer not found at {INSTALLER}"
    proc = await asyncio.create_subprocess_exec(
        "python", str(INSTALLER), *flags,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(ROOT),
    )
    raw, _ = await proc.communicate()
    return proc.returncode or 0, raw.decode("utf-8", errors="replace")


async def install_status(request):
    """GET /install/status — run a dry-run install to discover current state.

    Returns:
      {ok: true, output: "...", up_to_date: bool}
    """
    rc, out = await _run_installer("--dry-run")
    # Heuristic: if every component shows "already ... up-to-date" / "no change",
    # everything is in sync. Otherwise some component needs (re)installing.
    fresh_markers = (
        "already registered with the right path",
        "already installed and up-to-date",
        "hook script already up-to-date",
        "settings.json already registers this hook",
    )
    needs = "would write" in out or "would copy" in out or "would back up" in out
    return JSONResponse({
        "ok": rc == 0,
        "rc": rc,
        "output": out,
        "needs_install": needs,
    })


async def install_run(request):
    """POST /install/run {component?: 'mcp'|'skill'|'hook'|None} — install or
    update one or all components."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    component = (body.get("component") or "").strip().lower()
    flags: list[str] = []
    if component == "mcp":
        flags = ["--mcp"]
    elif component == "skill":
        flags = ["--skill"]
    elif component == "hook":
        flags = ["--hook"]
    elif component and component != "all":
        return JSONResponse({"ok": False, "error": f"unknown component: {component!r}"})
    rc, out = await _run_installer(*flags)
    log.info("install run %s rc=%d", flags or ["all"], rc)
    return JSONResponse({
        "ok": rc == 0,
        "rc": rc,
        "output": out,
    })
