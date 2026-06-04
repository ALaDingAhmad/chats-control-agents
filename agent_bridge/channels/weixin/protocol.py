"""
iLink Bot protocol layer — minimal port from Hermes' weixin.py for the
WeChat personal-account Bot API at https://ilinkai.weixin.qq.com.

Only what we need is ported here:
  - QR-code login flow (get qr → poll status → get token)
  - Long-poll getupdates for inbound text messages
  - sendmessage for outbound text replies (with context_token echo)

Skipped intentionally: media upload/download, AES CDN protocol, typing
indicators, markdown formatting, message dedup, group policy gates.

Reference upstream: F:/wslshare/hermes-agent/gateway/platforms/weixin.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import ssl
import struct
from typing import Any, Dict, Optional, Tuple

import aiohttp

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = None

log = logging.getLogger("weixin.proto")

# ── Constants ────────────────────────────────────────────────────────────
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
# (2 << 16) | (2 << 8) | 0 — must match Hermes' value or iLink rejects
APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_QR = "ilink/bot/get_bot_qrcode"
EP_QR_STATUS = "ilink/bot/get_qrcode_status"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"

# Message-shape constants from upstream
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TEXT = 1

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
QR_TIMEOUT_MS = 35_000


# ── Helpers ──────────────────────────────────────────────────────────────
def _random_uin() -> str:
    """Random base64-encoded uint32 used as X-WECHAT-UIN anti-replay header."""
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: Optional[str], body_len: int) -> Dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(body_len),
        "X-WECHAT-UIN": _random_uin(),
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(APP_CLIENT_VERSION),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _make_connector() -> Optional[aiohttp.TCPConnector]:
    if _SSL_CTX is None:
        return None
    return aiohttp.TCPConnector(ssl=_SSL_CTX)


def _build_session() -> aiohttp.ClientSession:
    """Caller owns the lifecycle; use `async with` around the call."""
    return aiohttp.ClientSession(trust_env=True, connector=_make_connector())


# ── API primitives ───────────────────────────────────────────────────────
async def _post_json(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: Optional[str],
    timeout_ms: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body_bytes = body.encode("utf-8")
    headers = _headers(token, len(body_bytes))
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(url, data=body_bytes, headers=headers, timeout=timeout) as r:
        raw = await r.text()
        if not r.ok:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {r.status}: {raw[:200]}")
        return json.loads(raw)


async def _get_json(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(APP_CLIENT_VERSION),
    }
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.get(url, headers=headers, timeout=timeout) as r:
        raw = await r.text()
        if not r.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {r.status}: {raw[:200]}")
        return json.loads(raw)


# ── QR login ─────────────────────────────────────────────────────────────
async def get_qrcode(
    session: aiohttp.ClientSession,
    *,
    base_url: str = ILINK_BASE_URL,
    bot_type: str = "3",
) -> Dict[str, str]:
    """Fetch a fresh QR for the iLink login flow.

    Returns dict:
      {
        "qrcode":             "<hex token used to poll status>",
        "qrcode_img_content": "<full liteapp URL the user scans>",
      }
    """
    resp = await _get_json(
        session,
        base_url=base_url,
        endpoint=f"{EP_GET_QR}?bot_type={bot_type}",
        timeout_ms=QR_TIMEOUT_MS,
    )
    return {
        "qrcode": str(resp.get("qrcode") or ""),
        "qrcode_img_content": str(resp.get("qrcode_img_content") or ""),
    }


async def poll_qrcode_status(
    session: aiohttp.ClientSession,
    *,
    qrcode: str,
    base_url: str = ILINK_BASE_URL,
) -> Dict[str, Any]:
    """Poll status of a previously fetched QR.

    Returns the raw upstream dict. Notable keys depending on state:
      status="wait" / "scaned" / "scaned_but_redirect" / "expired" / "confirmed"
      On "confirmed": ilink_bot_id, bot_token, baseurl, ilink_user_id
      On "scaned_but_redirect": redirect_host  (caller must switch base_url)
    """
    return await _get_json(
        session,
        base_url=base_url,
        endpoint=f"{EP_QR_STATUS}?qrcode={qrcode}",
        timeout_ms=QR_TIMEOUT_MS,
    )


# ── Runtime: getupdates / sendmessage ────────────────────────────────────
async def get_updates(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int = LONG_POLL_TIMEOUT_MS,
) -> Dict[str, Any]:
    """Long-poll for inbound messages.

    Pass the previous response's `get_updates_buf` back as `sync_buf` to
    advance the cursor. Returns the raw response; on aiohttp timeout we
    fabricate an empty response so callers don't have to handle it.
    """
    try:
        return await _post_json(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def send_text(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    text: str,
    context_token: Optional[str],
    client_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a plain-text reply. context_token MUST be echoed from the most
    recent inbound message for this peer so iLink routes it correctly.
    """
    if not text or not text.strip():
        raise ValueError("send_text: text must not be empty")
    if client_id is None:
        # Random client_id so iLink dedupes properly across retries
        client_id = base64.b64encode(secrets.token_bytes(12)).decode("ascii")
    msg: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    return await _post_json(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": msg},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


# ── Inbound message parsing ──────────────────────────────────────────────
def extract_text_and_meta(msg: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """From an inbound msg dict, return (sender_id, text, context_token) or
    None if the message has no plain text (e.g. image/voice we don't handle).

    Inbound shape:
      {
        from_user_id: "wxid_xxx",
        item_list: [{type: 1, text_item: {text: "..."}}],
        context_token: "...",
        ...
      }
    """
    sender = str(msg.get("from_user_id") or "")
    if not sender:
        return None
    items = msg.get("item_list") or []
    text_parts = []
    for it in items:
        if isinstance(it, dict) and it.get("type") == ITEM_TEXT:
            ti = it.get("text_item") or {}
            t = ti.get("text")
            if t:
                text_parts.append(str(t))
    if not text_parts:
        return None
    text = "\n".join(text_parts).strip()
    if not text:
        return None
    context_token = str(msg.get("context_token") or "")
    return sender, text, context_token
