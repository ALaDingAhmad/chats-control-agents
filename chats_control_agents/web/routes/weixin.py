"""WeChat iLink Bot HTTP routes: QR login UI, status, start QR, disconnect."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse

from ...channels.weixin import protocol as wx
from ...channels.weixin import state as wxs
from ..weixin_runtime import cancel_tasks_named, get_wx_state, qr_login_loop


log = logging.getLogger("web.weixin_route")


_WEIXIN_HTML_PATH = Path(__file__).resolve().parents[1] / "templates" / "weixin.html"
_WEIXIN_HTML: str | None = None


def _weixin_html() -> str:
    global _WEIXIN_HTML
    if _WEIXIN_HTML is None:
        _WEIXIN_HTML = _WEIXIN_HTML_PATH.read_text(encoding="utf-8")
    return _WEIXIN_HTML


async def weixin_page(request):
    return HTMLResponse(_weixin_html())


async def weixin_status(request):
    acct = wxs.load_account()
    connected = bool(acct and acct.get("bot_token"))
    _wx = get_wx_state()
    return JSONResponse({
        "connected": connected,
        "account_id": (acct or {}).get("ilink_bot_id"),
        "running": _wx.get("running", False),
        "last_error": _wx.get("last_error"),
        "qr_status": _wx.get("qr_status"),
        "qr_error": _wx.get("qr_error"),
        "qrcode_img_content": _wx.get("qrcode_img_content"),
        "last_peer_id": _wx.get("last_peer_id"),
    })


async def weixin_qr_start(request):
    """Kick off the QR login flow. Returns immediately after fetching the QR;
    actual status-polling happens in the background. Frontend polls
    /weixin/status to see progress."""
    _wx = get_wx_state()
    cancel_tasks_named("qr_login", "longpoll", "outbox_watch")
    _wx["running"] = False
    _wx["last_error"] = None
    wxs.clear_account()
    _wx["qr_status"] = "starting"
    _wx["qr_error"] = None
    try:
        async with wx._build_session() as session:
            qr = await wx.get_qrcode(session)
            if not qr.get("qrcode"):
                _wx["qr_status"] = "error"
                _wx["qr_error"] = "上游未返回 qrcode"
                return JSONResponse({"ok": False, "error": _wx["qr_error"]})
            _wx["qrcode"] = qr["qrcode"]
            _wx["qrcode_img_content"] = qr["qrcode_img_content"]
            _wx["qr_session_base_url"] = wx.ILINK_BASE_URL
            _wx["qr_status"] = "waiting"
            _wx["qr_started_at"] = datetime.now().isoformat(timespec="seconds")
        t = asyncio.create_task(qr_login_loop(), name="qr_login")
        _wx["tasks"].append(t)
        log.info("weixin qr started: %s", qr["qrcode"][:8])
        return JSONResponse({"ok": True, "qrcode_img_content": qr["qrcode_img_content"]})
    except Exception as e:
        log.exception("weixin qr_start failed: %s", e)
        _wx["qr_status"] = "error"
        _wx["qr_error"] = str(e)
        return JSONResponse({"ok": False, "error": str(e)})


async def weixin_disconnect(request):
    """Stop long-poll, clear stored account. User must scan again next time."""
    _wx = get_wx_state()
    cancel_tasks_named("longpoll", "outbox_watch", "qr_login")
    _wx["running"] = False
    _wx["qr_status"] = "idle"
    _wx["qrcode"] = None
    _wx["qrcode_img_content"] = None
    wxs.clear_account()
    log.info("weixin disconnected, credentials cleared")
    return JSONResponse({"ok": True})
