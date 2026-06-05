"""Starlette app assembly + lifespan hooks.

Run:
    python -m chats_control_agents.web.server
Then open http://127.0.0.1:8765/

Per-domain handlers live in routes/; long-lived runtime (WeChat long-poll,
autospawn worker) live in weixin_runtime.py and autospawn.py respectively.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

from ..core import sessions as sx
from ..core.paths import ROOT
from .autospawn import autospawn_worker
from .routes.chat import (
    dashboard,
    dashboard_status,
    get_history,
    index,
    new_session,
    poll,
    relay_push,
    send_message,
    settings,
)
from .routes.projects import (
    get_config_route,
    list_projects_route,
    update_workspace_route,
)
from .routes.install_status import install_run, install_status
from .routes.sessions import end_daemon_route, list_sessions_route, set_current_route
from .routes.weixin import (
    weixin_disconnect,
    weixin_page,
    weixin_qr_start,
    weixin_status,
)
from .weixin_runtime import (
    bootstrap_weixin,
    cancel_tasks_named,
    get_wx_state,
)


LOG_PATH = ROOT / "web_server.log"
logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("web")

# Run legacy single-session migration once at import time
sx.migrate_legacy_if_present()


@asynccontextmanager
async def _lifespan(app):
    # Startup
    await bootstrap_weixin()
    _wx = get_wx_state()
    _wx["tasks"].append(asyncio.create_task(autospawn_worker(), name="autospawn"))
    yield
    # Shutdown
    cancel_tasks_named("longpoll", "outbox_watch", "qr_login", "autospawn")


app = Starlette(
    routes=[
        Route("/", dashboard),
        Route("/chat", index),
        Route("/settings", settings),
        Route("/dashboard/status", dashboard_status),
        Route("/session/new", new_session, methods=["POST"]),
        Route("/history", get_history),
        Route("/send", send_message, methods=["POST"]),
        Route("/poll", poll),
        Route("/relay-push", relay_push, methods=["POST"]),
        Route("/sessions", list_sessions_route),
        Route("/session/use", set_current_route, methods=["POST"]),
        Route("/session/end", end_daemon_route, methods=["POST"]),
        Route("/projects", list_projects_route),
        Route("/config", get_config_route),
        Route("/config/workspace", update_workspace_route, methods=["POST"]),
        Route("/install/status", install_status),
        Route("/install/run", install_run, methods=["POST"]),
        Route("/weixin", weixin_page),
        Route("/weixin/status", weixin_status),
        Route("/weixin/qr/start", weixin_qr_start, methods=["POST"]),
        Route("/weixin/disconnect", weixin_disconnect, methods=["POST"]),
    ],
    lifespan=_lifespan,
)


def main() -> None:
    from ..core import config as cfg
    from ..core.paths import SESSIONS_ROOT
    log.info("=" * 60)
    log.info("web server starting on 127.0.0.1:8765")
    log.info("sessions_root=%s current=%s", SESSIONS_ROOT, sx.get_current())
    log.info("workspace_roots=%s", [str(r) for r in cfg.get_workspace_roots()])
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


if __name__ == "__main__":
    main()
