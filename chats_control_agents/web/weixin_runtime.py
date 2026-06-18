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

from ..channels.weixin import protocol as wx
from ..channels.weixin import state as wxs
from ..core import router
from ..core.paths import outbox_path
from ..core import sessions as sx


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
    "last_msg_at": None,           # epoch of most recent inbound message
    "last_poll_ok_at": None,       # epoch of most recent successful getupdates (ret=0)
    "last_error": None,            # most recent longpoll error string
    "alias_peer": {},              # alias → most-recent WeChat peer that wrote into it
}

# Per-alias outbox-watcher fingerprint memo (so we don't re-forward the same reply)
_outbox_seen: dict[str, str] = {}

# Per-peer typing-ticket cache (peer_id → (ticket, expires_at)). The bubble
# itself expires client-side after ~5s, so we keep tickets for 10 min and
# rely on _typing_keepalive_loop to re-send TYPING_START every few seconds.
_TYPING_TICKET_TTL = 600.0
_TYPING_KEEPALIVE_INTERVAL = 3.0   # < 5s so the bubble never blinks off
_TYPING_MAX_DURATION = 300.0       # safety cap — don't keepalive forever
_typing_tickets: dict[str, tuple[str, float]] = {}
_typing_tasks: dict[str, asyncio.Task] = {}


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
    # If we're tearing down longpoll/outbox_watch, any typing keepalive is
    # also stale — cancel them so they don't keep poking iLink in the dark.
    if "longpoll" in names or "outbox_watch" in names:
        for peer, t in list(_typing_tasks.items()):
            if not t.done():
                t.cancel()
            _typing_tasks.pop(peer, None)


def start_runtime_tasks(account: dict) -> None:
    """Spawn long-poll (inbound) and outbox-watcher (outbound) tasks."""
    cancel_tasks_named("longpoll", "outbox_watch")
    # Restore alias→peer mapping so the first outbox-push after restart can
    # reach the right WeChat user without waiting for them to write in again.
    restored = wxs.load_alias_peer()
    if restored:
        _wx["alias_peer"] = dict(restored)
        log.info("weixin: restored %d alias→peer mappings", len(restored))
    _wx["running"] = True
    t1 = asyncio.create_task(_inbound_longpoll(account), name="longpoll")
    t2 = asyncio.create_task(_outbox_watcher(account), name="outbox_watch")
    _wx["tasks"].extend([t1, t2])


async def _probe_token(account: dict) -> bool:
    """Lightweight token validity check via getconfig. Returns True if valid."""
    try:
        async with wx._build_session() as s:
            r = await wx.get_config(
                s,
                base_url=account["baseurl"],
                token=account["bot_token"],
                user_id=account.get("ilink_user_id", ""),
            )
        code = r.get("errcode", r.get("ret", 0))
        if code in (-14, -2):
            log.warning("weixin: token probe failed (errcode=%s: %s)", code, r.get("errmsg", ""))
            return False
        return True
    except Exception as e:
        log.warning("weixin: token probe error: %s", e)
        return False


async def bootstrap_weixin():
    """On server startup, resume long-poll if we have a stored account."""
    acct = wxs.load_account()
    if acct and acct.get("bot_token"):
        log.info("weixin: probing saved account %s", acct.get("ilink_bot_id"))
        if await _probe_token(acct):
            log.info("weixin: token valid, starting runtime")
            start_runtime_tasks(acct)
        else:
            log.warning("weixin: token invalid (session timeout), skipping longpoll")
            _wx["running"] = False
            _wx["last_error"] = "token 已失效（session timeout），需重新扫码"


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
    import time as _time
    token = account["bot_token"]
    base_url = account["baseurl"]
    sync_buf = ""
    backoff = 2
    _TOKEN_PROBE_INTERVAL = 300  # 每 5 分钟探测一次 token 有效性
    _last_probe = _time.time()
    log.info("weixin longpoll starting, base=%s", base_url)
    try:
        async with wx._build_session() as session:
            while _wx.get("running"):
                # 定期 token 探测：长时间没收到消息时检查 token 是否还有效
                now = _time.time()
                if (now - _last_probe >= _TOKEN_PROBE_INTERVAL
                        and _wx.get("last_msg_at") is None):
                    _last_probe = now
                    if not await _probe_token(account):
                        log.warning("weixin: token expired during longpoll, stopping")
                        _wx["running"] = False
                        _wx["last_error"] = "token 已失效（session timeout），需重新扫码"
                        return
                try:
                    resp = await wx.get_updates(
                        session, base_url=base_url, token=token, sync_buf=sync_buf,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    _wx["last_error"] = str(e)
                    log.warning("longpoll error: %s, backoff=%ds", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 2
                _wx["last_error"] = None

                ret = resp.get("ret")
                if ret not in (0, None):
                    errmsg = resp.get("errmsg") or ""
                    _wx["last_error"] = f"ret={ret} {errmsg}"
                    log.warning("longpoll ret=%s errmsg=%s", ret, errmsg)
                    if ret in (-14, -2):
                        log.warning("session stale, stopping longpoll")
                        _wx["running"] = False
                        return
                    await asyncio.sleep(2)
                    continue

                import time as _time
                _wx["last_poll_ok_at"] = _time.time()

                sync_buf = str(resp.get("get_updates_buf") or sync_buf)

                msgs = resp.get("msgs") or []
                for msg in msgs:
                    parsed = wx.extract_text_and_meta(msg)
                    if not parsed:
                        continue
                    sender, text, ctx_tok = parsed
                    _wx["last_peer_id"] = sender
                    _wx["last_msg_at"] = _time.time()
                    if ctx_tok:
                        wxs.set_context_token(sender, ctx_tok)
                    log.info("weixin inbound from=%s chars=%d", sender[:8], len(text))

                    # Channel-agnostic routing. The router decides what to do
                    # with the text (run a slash command, idle-gate to /proj,
                    # write to inbox, …) and returns either a reply string for
                    # us to deliver, or routed=True meaning the message hit
                    # the backend and we should show the typing bubble.
                    outcome = await router.route_inbound(
                        text, source=f"weixin:{sender[:8]}",
                    )

                    # Track which peer "owns" each alias so the outbox watcher
                    # knows who to send Claude's reply back to. This is a
                    # channel concern, not routing — only persist when the
                    # message actually went to the backend.
                    if outcome.routed and outcome.alias:
                        _wx.setdefault("alias_peer", {})[outcome.alias] = sender
                        wxs.set_alias_peer(outcome.alias, sender)

                    if outcome.reply is not None:
                        try:
                            r = await wx.send_text(
                                session, base_url=base_url, token=token,
                                to_user_id=sender, text=outcome.reply,
                                context_token=ctx_tok,
                            )
                            ret = r.get("ret")
                            if ret not in (0, None):
                                log.warning("weixin reply ret=%s errmsg=%s",
                                            ret, r.get("errmsg"))
                        except Exception as e:
                            log.warning("weixin reply failed: %s", e)
                        continue

                    if outcome.routed:
                        # Show "对方正在输入..." while Claude composes.
                        # outbox_watcher stops it after the reply is sent.
                        await _start_typing(
                            session, base_url=base_url, token=token,
                            peer=sender, ctx_tok=ctx_tok,
                        )
    except asyncio.CancelledError:
        log.info("longpoll cancelled")
        raise
    except Exception as e:
        log.exception("longpoll crashed: %s", e)
        _wx["running"] = False


# ── Typing indicator ─────────────────────────────────────────────────────
async def _fetch_typing_ticket(
    session, *, base_url: str, token: str, peer: str, ctx_tok: str | None,
) -> str | None:
    """Best-effort fetch + cache. Returns None on failure — typing is purely
    cosmetic, callers should not block on it."""
    cached = _typing_tickets.get(peer)
    if cached and cached[1] > asyncio.get_event_loop().time():
        return cached[0]
    try:
        resp = await wx.get_config(
            session, base_url=base_url, token=token,
            user_id=peer, context_token=ctx_tok,
        )
        ticket = str(resp.get("typing_ticket") or "")
        if ticket:
            _typing_tickets[peer] = (ticket, asyncio.get_event_loop().time() + _TYPING_TICKET_TTL)
            return ticket
    except Exception as e:
        log.debug("typing: getconfig failed peer=%s: %s", peer[:8], e)
    return None


async def _typing_keepalive_loop(
    session, *, base_url: str, token: str, peer: str, ticket: str,
) -> None:
    """Re-send TYPING_START every few seconds until cancelled. Bubble lifetime
    on the WeChat client is ~5s so we tick at 3s."""
    elapsed = 0.0
    try:
        while elapsed < _TYPING_MAX_DURATION:
            try:
                await wx.send_typing(
                    session, base_url=base_url, token=token,
                    to_user_id=peer, typing_ticket=ticket, status=wx.TYPING_START,
                )
            except Exception as e:
                log.debug("typing: keepalive send failed peer=%s: %s", peer[:8], e)
            await asyncio.sleep(_TYPING_KEEPALIVE_INTERVAL)
            elapsed += _TYPING_KEEPALIVE_INTERVAL
    except asyncio.CancelledError:
        raise


async def _start_typing(
    session, *, base_url: str, token: str, peer: str, ctx_tok: str | None,
) -> None:
    """Begin showing '对方正在输入...' for peer. Idempotent — replaces any
    existing keepalive task for the same peer."""
    # Cancel any existing keepalive for this peer first (could be stale)
    existing = _typing_tasks.get(peer)
    if existing and not existing.done():
        existing.cancel()
    ticket = await _fetch_typing_ticket(
        session, base_url=base_url, token=token, peer=peer, ctx_tok=ctx_tok,
    )
    if not ticket:
        return
    try:
        await wx.send_typing(
            session, base_url=base_url, token=token,
            to_user_id=peer, typing_ticket=ticket, status=wx.TYPING_START,
        )
    except Exception as e:
        log.debug("typing: initial start failed peer=%s: %s", peer[:8], e)
        return
    t = asyncio.create_task(
        _typing_keepalive_loop(
            session, base_url=base_url, token=token, peer=peer, ticket=ticket,
        ),
        name=f"typing_{peer[:8]}",
    )
    _typing_tasks[peer] = t


async def _stop_typing(
    session, *, base_url: str, token: str, peer: str,
) -> None:
    """End the typing indicator. Safe to call when no indicator is active."""
    task = _typing_tasks.pop(peer, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    cached = _typing_tickets.get(peer)
    if not cached:
        return
    ticket = cached[0]
    try:
        await wx.send_typing(
            session, base_url=base_url, token=token,
            to_user_id=peer, typing_ticket=ticket, status=wx.TYPING_STOP,
        )
    except Exception as e:
        log.debug("typing: stop failed peer=%s: %s", peer[:8], e)


# ── Outbox watcher (per-alias outbound) ──────────────────────────────────
async def _outbox_watcher(account: dict):
    """Watch every session's outbox; forward fresh replies back to the WeChat
    peer that most recently sent INTO that session. _wx['alias_peer'] tracks
    that mapping (set by _inbound_longpoll when a WeChat message arrives).
    """
    token = account["bot_token"]
    base_url = account["baseurl"]
    # Prime _outbox_seen with whatever is already on disk so we don't replay
    # stale outbox.txt content on restart. _outbox_seen is in-memory only;
    # without this, any leftover reply (e.g. from a browser /send test before
    # WeChat was connected) would be forwarded as "new" the first time
    # alias_peer gets populated. Use the same fingerprint format as the loop.
    primed = 0
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
        _outbox_seen[alias] = stamp + "|" + reply[:120]
        primed += 1
    log.info("weixin outbox_watcher starting (multi-session), primed=%d", primed)
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
                    sent_ok = False
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
                            sent_ok = True
                    except Exception as e:
                        log.warning("send_text failed: %s: %s", type(e).__name__, e or repr(e))
                    # Stop the typing bubble regardless of send result —
                    # leaving it on after a failed send would be misleading.
                    await _stop_typing(
                        session, base_url=base_url, token=token, peer=peer,
                    )
                    # Only mark seen on successful delivery. On failure (network
                    # blip, token expired, iLink ret!=0) leave fp un-memoed so
                    # the next loop iteration retries. Permanent errors will
                    # then loop noisily — that's by design; check the log
                    # rather than silently losing the message.
                    if sent_ok:
                        _outbox_seen[alias] = fp
    except asyncio.CancelledError:
        log.info("outbox_watcher cancelled")
        raise
    except Exception as e:
        log.exception("outbox_watcher crashed: %s", e)
