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


# HTML pages lazy-loaded from templates/ on first request
_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
_INDEX_HTML: str | None = None
_DASHBOARD_HTML: str | None = None
_SETTINGS_HTML: str | None = None


def _index_html() -> str:
    global _INDEX_HTML
    if _INDEX_HTML is None:
        _INDEX_HTML = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return _INDEX_HTML


def _dashboard_html() -> str:
    global _DASHBOARD_HTML
    if _DASHBOARD_HTML is None:
        _DASHBOARD_HTML = (_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
    return _DASHBOARD_HTML


def _settings_html() -> str:
    global _SETTINGS_HTML
    if _SETTINGS_HTML is None:
        _SETTINGS_HTML = (_TEMPLATES_DIR / "settings.html").read_text(encoding="utf-8")
    return _SETTINGS_HTML


async def index(request):
    """Chat UI, served from / by legacy code and now from /chat."""
    return HTMLResponse(_index_html())


async def dashboard(request):
    """Service overview landing page, served from /."""
    return HTMLResponse(_dashboard_html())


async def settings(request):
    """Tabbed settings page: workspace, weixin, system."""
    return HTMLResponse(_settings_html())


async def new_session(request):
    """POST /session/new {mode: 'chat'|'project', project_cwd?: str, backend?: str}
    Dashboard "start new session" button hits this. Spawns a fresh daemon
    with auto-generated alias and marks it current.

    backend 缺省 "claude_code"，可传 "hermes_acp"——决定起哪个 daemon。
    """
    from ..spawn_helpers import spawn_new_session
    body = await request.json()
    mode = (body.get("mode") or "").strip()
    project_cwd = (body.get("project_cwd") or "").strip() or None
    backend = (body.get("backend") or "claude_code").strip()
    result = await spawn_new_session(mode, project_cwd, backend=backend)
    return JSONResponse(result)


async def dashboard_status(request):
    """Aggregated state for the dashboard cards. Keeps the page to one
    request instead of fanning out to /sessions + /weixin/status + /config."""
    import json as _json
    from pathlib import Path as _Path

    from ...channels.weixin import state as wxs
    from ...core import config as cfg
    from ...core.paths import ROOT
    from ..weixin_runtime import get_wx_state
    sessions = sx.list_sessions()
    online = sum(1 for s in sessions if s.get("online"))
    acct = wxs.load_account()
    wx_state = get_wx_state()

    # MCP registration check: does ~/.claude.json have a cca-msg entry
    # pointing at OUR mcp_bridge.py?
    mcp_registered = False
    try:
        claude_json = _Path.home() / ".claude.json"
        if claude_json.exists():
            d = _json.loads(claude_json.read_text(encoding="utf-8"))
            wc = (d.get("mcpServers") or {}).get("cca-msg") or {}
            args = wc.get("args") or []
            expected = str(ROOT / "chats_control_agents" / "backends" / "claude_code" / "mcp_bridge.py")
            expected_norm = expected.replace("\\", "/")
            mcp_registered = any(
                str(a).replace("\\", "/") == expected_norm for a in args
            )
    except Exception:
        pass

    return JSONResponse({
        "current": sx.get_current(),
        "sessions_total": len(sessions),
        "sessions_online": online,
        "claude": {
            "mcp_registered": mcp_registered,
        },
        "weixin": {
            "connected": bool(acct and acct.get("bot_token")),
            "running": wx_state.get("running", False),
            "account_id": (acct or {}).get("ilink_bot_id"),
        },
        "workspace_roots": [str(r) for r in cfg.get_workspace_roots()],
    })


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
    if not alias:
        return JSONResponse({
            "ok": False, "reason": "no_session",
            "hint": "还没有活跃会话，请到 dashboard 创建一个，或用 /proj 选项目。",
        })
    log.info("send to %s: %d chars", alias, len(text))
    # Revive daemon on demand if it died while idle.
    from ..spawn_helpers import ensure_daemon_alive
    alive = await ensure_daemon_alive(alias)
    if not alive:
        history = load_history(alias)
        history.append({"role": "user", "text": text, "ts": now_iso()})
        history.append({
            "role": "assistant",
            "text": "⚠️ agent 拉起失败，请稍后再试或检查 daemon.log。",
            "ts": now_iso(), "source": "system",
        })
        save_history(history, alias)
        return JSONResponse({"ok": False, "reason": "daemon_unavailable", "alias": alias})
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
