"""Channel-agnostic inbound routing.

Channels (weixin, future feishu/slack/…) call route_inbound(text, source)
when a user-typed message arrives. The router decides:

  - Slash command?           → run it, return its reply.
  - No current session?      → return a "create one" hint.
  - Current session idle > IDLE_THRESHOLD?
                             → return /proj listing so the user explicitly
                               picks continue or new (daemon untouched).
  - Daemon couldn't be revived? → return a "spawn failed" notice.
  - Otherwise                → write text to the session inbox + append
                               history. Return outcome.routed=True so the
                               channel can do channel-specific UI work
                               (e.g. WeChat's "对方正在输入..." bubble).

The router never touches network sockets or channel protocols. All replies
come back as plain strings; the channel decides how to deliver them.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from . import commands as cmd
from . import sessions as sx
from .history import load_history, now_iso, save_history
from .paths import inbox_path
from .spawn import ensure_daemon_alive


log = logging.getLogger("core.router")

# How long a session can sit silent before an inbound message stops
# auto-reviving it. Past this, the user gets the /proj listing back and
# explicitly picks continue-or-new.
IDLE_THRESHOLD_SECS = 2 * 3600


@dataclass
class RouteOutcome:
    """What the channel should do with the inbound message.

    Exactly one of `reply` / `routed` carries the meaningful signal:
      - reply: send this text back to the user, then stop.
      - routed: message was written into the session inbox; the channel
                may now perform channel-specific UI (typing indicator…).
    `alias` is the session the message was bound to (or would have been),
    so the channel can update its own per-alias bookkeeping.
    """
    reply: Optional[str] = None
    routed: bool = False
    alias: Optional[str] = None


async def route_inbound(text: str, source: str) -> RouteOutcome:
    """Route a user-typed inbound message.

    `text`   the raw user text, exactly as the channel received it.
    `source` short tag identifying where it came from, e.g. "weixin:o9cq809U"
             or "web". Stored verbatim in history.json.
    """
    # Slash command short-circuits everything.
    if cmd.is_command(text):
        return RouteOutcome(reply=cmd.handle_command(text))

    # Passthrough: //handoff → /handoff for the agent to handle.
    text = cmd.strip_passthrough_prefix(text)

    alias = sx.get_current()
    if not alias:
        return RouteOutcome(
            reply="⚠️ 还没有活跃会话，请到 dashboard 创建一个。",
        )

    # Idle gate. Daemon is not touched — once the user picks a project via
    # /proj, the existing revive-on-demand path handles spawn.
    last_active = sx._last_active(alias) or 0.0
    idle_secs = time.time() - last_active if last_active else 0.0
    if last_active and idle_secs > IDLE_THRESHOLD_SECS:
        hrs = int(idle_secs // 3600)
        log.info("idle-gate[%s]: idle=%.0fs, prompting /proj", alias, idle_secs)
        prompt = (
            f"⚠️ 会话 {alias!r} 已 {hrs} 小时没动静。\n"
            "选项目继续聊或开新会话：\n\n"
            + cmd.handle_command("/proj")
        )
        return RouteOutcome(reply=prompt, alias=alias)

    # Revive dead daemon on demand.
    alive = await ensure_daemon_alive(alias)
    if not alive:
        return RouteOutcome(
            reply="⚠️ agent 拉起失败，请稍后再试。",
            alias=alias,
        )

    # Hand off to the backend by writing the inbox file. mcp_bridge inside
    # child claude is polling this at 0.5s cadence.
    p = inbox_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(text, encoding="utf-8")
    except Exception as e:
        log.warning("inbox write failed for %s: %s", alias, e)
        return RouteOutcome(
            reply="⚠️ 内部错误：消息无法写入会话，请稍后重试。",
            alias=alias,
        )

    history = load_history(alias)
    history.append({
        "role": "user", "text": text, "ts": now_iso(), "source": source,
    })
    save_history(history, alias)
    return RouteOutcome(routed=True, alias=alias)
