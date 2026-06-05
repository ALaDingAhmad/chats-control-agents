"""WeChat iLink Bot runtime: in-memory state, QR login loop, inbound long-poll,
outbox watcher.

Lives outside any single request handler — these are long-lived asyncio
tasks created at server startup (if a saved account exists) or when the user
completes a QR login. State is kept in the module-level `_wx` dict so route
handlers in routes/weixin.py can read it for status pages.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ..channels.weixin import protocol as wx
from ..channels.weixin import state as wxs
from ..core import commands as cmd
from ..core import sessions as sx
from ..core.paths import inbox_path, outbox_path
from .helpers import load_history, now_iso, save_history


log = logging.getLogger("web.weixin")


# In-memory WeChat runtime state. Mutated by handlers and background tasks.
_wx: dict = {
    "qrcode": None,                # hex token returned by get_qrcode
    "qrcode_img_content": None,    # full URL the QR encodes
    "qr_session_base_url": wx.ILINK_BASE_URL,  # may redirect mid-login
    "qr_status": "idle",           # idle / waiting / scaned / confirmed / expired / error
    "qr_error": None,
    "qr_started_at": None,
    "last_peer_id": None,          # most recent inbound sender — also used by /weixin/status
    "tasks": [],                   # asyncio.Task list (longpoll, outbox_watch, qr_login, autospawn)
    "outbox_last_pushed": "",      # fingerprint of last outbox we forwarded (legacy single-session)
    "running": False,              # whether the longpoll loop is alive
    "alias_peer": {},              # alias → most-recent WeChat peer that wrote into it
}

# Per-alias outbox-watcher fingerprint memo (so we don't re-forward the same reply)
_outbox_seen: dict[str, str] = {}


def get_wx_state() -> dict:
    return _wx


def cancel_tasks_named(*names: str) -> None:
    keep = []
    for t in _wx.get("tasks", []):
        if t.get_name() in names and not t.done():
            t.cancel()
        else:
            keep.append(t)
    _wx["tasks"] = keep


def start_runtime_tasks(account: dict) -> None:
    """Spawn long-poll (inbound) and outbox-watcher (outbound) tasks."""
    cancel_tasks_named("longpoll", "outbox_watch")
    _wx["running"] = True
    t1 = asyncio.create_task(_inbound_longpoll(account), name="longpoll")
    t2 = asyncio.create_task(_outbox_watcher(account), name="outbox_watch")
    _wx["tasks"].extend([t1, t2])


async def bootstrap_weixin():
    """On server startup, resume long-poll if we have a stored account."""
    acct = wxs.load_account()
    if acct and acct.get("bot_token"):
        log.info("weixin: resuming saved account %s", acct.get("ilink_bot_id"))
        start_runtime_tasks(acct)


# ── QR login loop ───────────────────────────────────────────────────────
async def qr_login_loop():
    """Poll iLink for QR status until confirmed / expired / cancelled.
    On confirmed, save credentials and spawn long-poll + outbox watcher.
    """
    try:
        deadline_secs = 480  # ~8 minutes total
        start = asyncio.get_event_loop().time()
        async with wx._build_session() as session:
            while asyncio.get_event_loop().time() - start < deadline_secs:
                qrcode = _wx.get("qrcode")
                if not qrcode:
                    return
                base = _wx.get("qr_session_base_url", wx.ILINK_BASE_URL)
                try:
                    resp = await wx.poll_qrcode_status(
                        session, qrcode=qrcode, base_url=base,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("qr poll error: %s", e)
                    await asyncio.sleep(1)
                    continue

                status = str(resp.get("status") or "wait")
                if status == "wait":
                    _wx["qr_status"] = "waiting"
                elif status == "scaned":
                    _wx["qr_status"] = "scaned"
                elif status == "scaned_but_redirect":
                    redirect_host = str(resp.get("redirect_host") or "")
                    if redirect_host:
                        _wx["qr_session_base_url"] = f"https://{redirect_host}"
                        log.info("qr redirected to %s", redirect_host)
                elif status == "expired":
                    _wx["qr_status"] = "expired"
                    log.info("qr expired")
                    return
                elif status == "confirmed":
                    account = {
                        "ilink_bot_id": str(resp.get("ilink_bot_id") or ""),
                        "bot_token": str(resp.get("bot_token") or ""),
                        "baseurl": str(resp.get("baseurl") or wx.ILINK_BASE_URL),
                        "ilink_user_id": str(resp.get("ilink_user_id") or ""),
                    }
                    if not account["ilink_bot_id"] or not account["bot_token"]:
                        _wx["qr_status"] = "error"
                        _wx["qr_error"] = "上游 confirmed 但凭证字段缺失"
                        return
                    wxs.save_account(account)
                    _wx["qr_status"] = "confirmed"
                    _wx["qrcode"] = None
                    log.info("weixin connected as %s", account["ilink_bot_id"])
                    start_runtime_tasks(account)
                    return
                await asyncio.sleep(1)
        _wx["qr_status"] = "expired"
    except asyncio.CancelledError:
        log.info("qr_login_loop cancelled")
        raise
    except Exception as e:
        log.exception("qr_login_loop crashed: %s", e)
        _wx["qr_status"] = "error"
        _wx["qr_error"] = str(e)


# ── Inbound long-poll ────────────────────────────────────────────────────
async def _inbound_longpoll(account: dict):
    """Long-poll iLink getupdates, write inbound text into the alias inbox.

    sync_buf advances each round so we don't re-receive the same messages.
    Records the latest peer's context_token so replies route correctly.
    """
    token = account["bot_token"]
    base_url = account["baseurl"]
    sync_buf = ""
    backoff = 2
    log.info("weixin longpoll starting, base=%s", base_url)
    try:
        async with wx._build_session() as session:
            while _wx.get("running"):
                try:
                    resp = await wx.get_updates(
                        session, base_url=base_url, token=token, sync_buf=sync_buf,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("longpoll error: %s, backoff=%ds", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 2

                ret = resp.get("ret")
                if ret not in (0, None):
                    errmsg = resp.get("errmsg") or ""
                    log.warning("longpoll ret=%s errmsg=%s", ret, errmsg)
                    if ret in (-14, -2):
                        log.warning("session stale, stopping longpoll")
                        _wx["running"] = False
                        return
                    await asyncio.sleep(2)
                    continue

                sync_buf = str(resp.get("get_updates_buf") or sync_buf)

                msgs = resp.get("msgs") or []
                for msg in msgs:
                    parsed = wx.extract_text_and_meta(msg)
                    if not parsed:
                        continue
                    sender, text, ctx_tok = parsed
                    _wx["last_peer_id"] = sender
                    if ctx_tok:
                        wxs.set_context_token(sender, ctx_tok)
                    log.info("weixin inbound from=%s chars=%d", sender[:8], len(text))

                    # Slash command → answer directly in-channel.
                    if cmd.is_command(text):
                        reply = cmd.handle_command(text)
                        try:
                            resp = await wx.send_text(
                                session, base_url=base_url, token=token,
                                to_user_id=sender, text=reply, context_token=ctx_tok,
                            )
                            ret = resp.get("ret")
                            if ret not in (0, None):
                                log.warning("weixin command %r reply ret=%s errmsg=%s",
                                            text[:30], ret, resp.get("errmsg"))
                        except Exception as e:
                            log.warning("weixin command reply failed: %s", e)
                        continue

                    # Regular message → route to currently selected session.
                    text = cmd.strip_passthrough_prefix(text)
                    alias = sx.get_current()
                    if not alias:
                        try:
                            await wx.send_text(
                                session, base_url=base_url, token=token,
                                to_user_id=sender,
                                text="⚠️ 还没有活跃会话，请到 dashboard 创建一个。",
                                context_token=ctx_tok,
                            )
                        except Exception as e:
                            log.warning("weixin no-session notify failed: %s", e)
                        continue
                    _wx.setdefault("alias_peer", {})[alias] = sender
                    # Revive daemon on demand if it died while idle.
                    from .spawn_helpers import ensure_daemon_alive
                    alive = await ensure_daemon_alive(alias)
                    if not alive:
                        try:
                            await wx.send_text(
                                session, base_url=base_url, token=token,
                                to_user_id=sender,
                                text="⚠️ agent 拉起失败，请稍后再试。",
                                context_token=ctx_tok,
                            )
                        except Exception as e:
                            log.warning("weixin notify spawn-fail failed: %s", e)
                        continue
                    inbox_path(alias).parent.mkdir(parents=True, exist_ok=True)
                    try:
                        inbox_path(alias).write_text(text, encoding="utf-8")
                    except Exception as e:
                        log.warning("inbox write failed: %s", e)
                    history = load_history(alias)
                    history.append({
                        "role": "user", "text": text, "ts": now_iso(),
                        "source": f"weixin:{sender[:8]}",
                    })
                    save_history(history, alias)
    except asyncio.CancelledError:
        log.info("longpoll cancelled")
        raise
    except Exception as e:
        log.exception("longpoll crashed: %s", e)
        _wx["running"] = False


# ── Outbox watcher (per-alias outbound) ──────────────────────────────────
async def _outbox_watcher(account: dict):
    """Watch every session's outbox; forward fresh replies back to the WeChat
    peer that most recently sent INTO that session. _wx['alias_peer'] tracks
    that mapping (set by _inbound_longpoll when a WeChat message arrives).
    """
    token = account["bot_token"]
    base_url = account["baseurl"]
    log.info("weixin outbox_watcher starting (multi-session)")
    try:
        async with wx._build_session() as session:
            while _wx.get("running"):
                await asyncio.sleep(0.5)
                for sess in sx.list_sessions():
                    alias = sess["alias"]
                    p = outbox_path(alias)
                    if not p.exists():
                        continue
                    try:
                        content = p.read_text(encoding="utf-8").strip()
                    except Exception:
                        continue
                    if not content:
                        continue
                    lines = content.split("\n", 1)
                    stamp = lines[0] if content.startswith("[") else ""
                    reply = lines[1] if len(lines) > 1 else content
                    fp = stamp + "|" + reply[:120]
                    if _outbox_seen.get(alias) == fp:
                        continue

                    peer = (_wx.get("alias_peer") or {}).get(alias)
                    if not peer:
                        # No WeChat user has spoken into this session yet —
                        # browser still gets it via /poll. Do NOT mark seen:
                        # if/when a peer later writes in, we want this reply
                        # to be forwarded then. Skipping send_text is cheap
                        # (no network call), so re-checking each loop iter
                        # is fine. Watch out for one edge: outbox.txt that
                        # gets overwritten in the meantime — fp changes too,
                        # so the new one will be forwarded normally.
                        continue
                    ctx_tok = wxs.get_context_token(peer)
                    try:
                        resp = await wx.send_text(
                            session, base_url=base_url, token=token,
                            to_user_id=peer, text=reply, context_token=ctx_tok,
                        )
                        ret = resp.get("ret")
                        if ret not in (0, None):
                            log.warning("send_text ret=%s errmsg=%s",
                                        ret, resp.get("errmsg"))
                        else:
                            log.info("weixin out[%s] to=%s chars=%d",
                                     alias, peer[:8], len(reply))
                    except Exception as e:
                        log.warning("send_text failed: %s", e)
                    _outbox_seen[alias] = fp
    except asyncio.CancelledError:
        log.info("outbox_watcher cancelled")
        raise
    except Exception as e:
        log.exception("outbox_watcher crashed: %s", e)
