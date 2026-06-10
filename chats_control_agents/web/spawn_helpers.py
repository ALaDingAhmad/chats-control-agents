"""Web-layer spawn helpers.

The OS-level "spawn detached daemon" and "ensure daemon alive" primitives
moved to core/spawn.py; this module re-exports them for backward compat
and keeps the dashboard-driven `spawn_new_session` (which is web-flow
specific — it预写 meta、watch ready marker、把新 alias 设成 current）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ..core.spawn import ensure_daemon_alive, spawn_daemon_detached  # noqa: F401 (re-export)


log = logging.getLogger("web.spawn")

# 跟 core.spawn / hermes_acp daemon 共用同一组 marker 约定。daemon 起来后
# 会 touch `~/.claude/.chats-loop-active-<alias>`——marker 一出现就是 ready。
# 60s 给冷启动留余地（hermes_acp 首启实测 9s，claude_code TUI 起 + skill init
# 通常 15-40s）。
_READY_TIMEOUT_SECS = 60.0
_POLL_INTERVAL_SECS = 0.5
_MARKER_DIR = Path.home() / ".claude"

# 已知 backend 集合，用于校验前端传值。新增 backend 时同步加这里
# （或之后改成读 core.spawn._BACKEND_DAEMON_MODULES 的 keys）。
_KNOWN_BACKENDS = {"claude_code", "hermes_acp"}


# Back-compat alias for callers that still import the leading-underscore name.
_spawn_daemon_detached = spawn_daemon_detached


def _marker_path(alias: str) -> Path:
    return _MARKER_DIR / f".chats-loop-active-{alias}"


# ── Dashboard-driven new session ─────────────────────────────────────────
async def spawn_new_session(
    mode: str,
    project_cwd: str | None = None,
    backend: str = "claude_code",
) -> dict:
    """Create a fresh session for the user from the dashboard.

    mode='chat'    → cwd = home, alias = <home basename>-<MMDD-HHMM>
    mode='project' → cwd = project_cwd, alias = <basename>-<MMDD-HHMM>
    backend        → 决定起哪个 daemon ("claude_code" / "hermes_acp")

    Flow:
      1. 算出 alias + cwd
      2. 预写 meta.json（含 backend 字段）——这一步关键，否则 spawn 时
         _resolve_daemon_module 看不到 backend 字段会兜底 claude_code
      3. spawn_daemon_detached → 起进程
      4. 等 `~/.claude/.chats-loop-active-<alias>` marker 出现（统一就绪信号）
      5. set_current → 返回 {ok, alias}
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

    # 等 marker 文件出现——跟 ensure_daemon_alive/watch_ready 用同一信号
    marker = _marker_path(alias)
    deadline = time.time() + _READY_TIMEOUT_SECS
    while time.time() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_SECS)
        if marker.exists():
            try:
                set_current(alias)
            except Exception as e:
                log.warning("spawn_new_session[%s]: set_current failed: %s", alias, e)
            log.info("spawn_new_session[%s]: ready", alias)
            return {"ok": True, "alias": alias, "backend": backend}

    log.warning("spawn_new_session[%s]: ready timeout (%.0fs)", alias, _READY_TIMEOUT_SECS)
    return {"ok": False, "error": "daemon ready timeout", "alias": alias}
