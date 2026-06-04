"""Chat I/O routes: index page, history, send/poll, relay-push from hooks."""
from __future__ import annotations

import logging
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse

from ...core import commands as cmd
from ...core import sessions as sx
from ...core.paths import inbox_path, outbox_path
from ..helpers import load_history, now_iso, save_history


log = logging.getLogger("web.chat")


# HTML page lazy-loaded from templates/ on first request
_INDEX_HTML_PATH = Path(__file__).resolve().parents[1] / "templates" / "index.html"
_INDEX_HTML: str | None = None


def _index_html() -> str:
    global _INDEX_HTML
    if _INDEX_HTML is None:
        _INDEX_HTML = _INDEX_HTML_PATH.read_text(encoding="utf-8")
    return _INDEX_HTML


async def index(request):
    return HTMLResponse(_index_html())


async def get_history(request):
    """?alias=X overrides current; defaults to current selection."""
    alias = request.query_params.get("alias") or sx.get_current()
    return JSONResponse(load_history(alias))


async def send_message(request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "reason": "empty"})

    # Slash commands handled in-process; never go to the agent.
    if cmd.is_command(text):
        reply = cmd.handle_command(text)
        alias = sx.get_current()
        history = load_history(alias)
        ts = now_iso()
        history.append({"role": "user", "text": text, "ts": ts, "source": "browser:command"})
        history.append({"role": "assistant", "text": reply, "ts": ts, "source": "command"})
        save_history(history, alias)
        log.info("send: command %r", text[:50])
        return JSONResponse({"ok": True, "command": True, "reply": reply})

    # Regular message: route to currently selected session.
    # `//foo` is the passthrough escape — strip one slash so child agent sees /foo.
    text = cmd.strip_passthrough_prefix(text)
    alias = sx.get_current()
    log.info("send to %s: %d chars", alias, len(text))
    # Clear stale outbox FIRST so /poll won't return last turn's reply as
    # if it were the answer to this new message.
    try:
        outbox_path(alias).write_text("", encoding="utf-8")
    except Exception as e:
        log.warning("clear outbox failed: %s", e)
    inbox_path(alias).parent.mkdir(parents=True, exist_ok=True)
    inbox_path(alias).write_text(text, encoding="utf-8")
    history = load_history(alias)
    history.append({"role": "user", "text": text, "ts": now_iso()})
    save_history(history, alias)
    return JSONResponse({"ok": True, "alias": alias})


async def relay_push(request):
    """PreToolUse hook posts assistant narration harvested from the agent's
    transcript. The hook may pass `alias` directly (preferred); otherwise we
    use the current selection."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "reason": "empty"})
    alias = body.get("alias") or sx.get_current()

    history = load_history(alias)
    last_assistant = next(
        (m for m in reversed(history) if m["role"] == "assistant"), None
    )
    if last_assistant and last_assistant["text"].strip() in text:
        if last_assistant["text"].strip() == text:
            log.info("relay_push[%s]: dedup exact match", alias)
            return JSONResponse({"ok": True, "appended": False})
        extra = text.replace(last_assistant["text"].strip(), "").strip()
        if extra:
            text = extra
        else:
            return JSONResponse({"ok": True, "appended": False})

    history.append({
        "role": "assistant", "text": text, "ts": now_iso(),
        "fp": "hook|" + text[:80],
        "source": body.get("source") or "hook",
    })
    save_history(history, alias)
    log.info("relay_push[%s]: appended %d chars", alias, len(text))
    return JSONResponse({"ok": True, "appended": True, "alias": alias})


async def poll(request):
    """Check if outbox has a fresh reply. Returns {changed, history, alias}.

    Reads the outbox of the CURRENT selected session, so switching session
    in another tab automatically swings poll to the new outbox.
    """
    alias = request.query_params.get("alias") or sx.get_current()
    p = outbox_path(alias)
    if not p.exists():
        return JSONResponse({"changed": False, "history": load_history(alias), "alias": alias})
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        return JSONResponse({"changed": False, "history": load_history(alias), "alias": alias})

    # outbox format from mcp_bridge.py: "[HH:MM:SS]\n<reply>"
    lines = content.split("\n", 1)
    stamp = lines[0] if content.startswith("[") else ""
    reply = lines[1] if len(lines) > 1 else content

    history = load_history(alias)
    last_assistant = next(
        (m for m in reversed(history) if m["role"] == "assistant"), None
    )
    fingerprint = stamp + "|" + reply
    if last_assistant and last_assistant.get("fp") == fingerprint:
        return JSONResponse({"changed": False, "history": history, "alias": alias})

    history.append({
        "role": "assistant", "text": reply, "ts": now_iso(), "fp": fingerprint,
    })
    save_history(history, alias)
    log.info("poll[%s]: new reply %d chars", alias, len(reply))
    return JSONResponse({"changed": True, "history": history, "alias": alias})
