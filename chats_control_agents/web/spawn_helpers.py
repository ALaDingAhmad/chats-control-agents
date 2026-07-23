"""Web-layer spawn helpers.

The OS-level "spawn detached daemon" and "ensure daemon alive" primitives
moved to core/spawn.py; this module re-exports them for backward compat
and keeps the dashboard-driven `spawn_new_session` (which is web-flow
specific — 预写 meta、spawn、set_current）。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..core.spawn import ensure_daemon_alive, spawn_daemon_detached  # noqa: F401 (re-export)


log = logging.getLogger("web.spawn")

_KNOWN_BACKENDS = {"claude_channel", "hermes_acp"}  # claude_code 已删除 2026-07-23

_spawn_daemon_detached = spawn_daemon_detached


# ── Dashboard-driven new session ─────────────────────────────────────────
async def spawn_new_session(
    mode: str,
    project_cwd: str | None = None,
    backend: str = "claude_channel",
) -> dict:
    """Create a fresh session for the user from the dashboard.

    mode='chat'    → cwd = home, alias = <home basename>-<MMDD-HHMM>
    mode='project' → cwd = project_cwd, alias = <basename>-<MMDD-HHMM>
    backend        → 决定起哪个 daemon ("claude_channel" / "hermes_acp")

    Returns immediately after spawning. Daemon writes progress to outbox.
    """
    from ..core.sessions import (
        create_session_dir,
        make_alias_for_cwd,
        set_current,
    )
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

    if backend not in _KNOWN_BACKENDS:
        return {"ok": False, "error": f"unknown backend: {backend!r}"}

    alias = make_alias_for_cwd(cwd)
    log.info("spawn_new_session[%s]: mode=%s backend=%s cwd=%s", alias, mode, backend, cwd)

    # 预写 meta，spawn_daemon_detached 内部按 meta.backend 选 daemon 模块
    try:
        create_session_dir(alias, cwd, backend=backend)
    except ValueError as e:
        return {"ok": False, "error": f"create session failed: {e}"}

    pid = spawn_daemon_detached(alias, cwd)
    if not pid:
        return {"ok": False, "error": "daemon spawn failed", "alias": alias}

    # Daemon writes progress to outbox.txt via PTY parsing — no need to
    # block here waiting for a marker file. Just confirm process is alive
    # after a brief settle, then return immediately so the dashboard can
    # show outbox-streamed progress to the user.
    await asyncio.sleep(1.0)
    try:
        set_current(alias)
    except Exception as e:
        log.warning("spawn_new_session[%s]: set_current failed: %s", alias, e)
    log.info("spawn_new_session[%s]: daemon pid=%s, returning (progress via outbox)", alias, pid)
    return {"ok": True, "alias": alias, "backend": backend}
