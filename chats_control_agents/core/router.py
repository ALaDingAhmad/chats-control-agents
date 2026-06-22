"""Channel-agnostic inbound routing."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import commands as cmd
from . import sessions as sx
from .history import load_history, now_iso, save_history
from .paths import control_mode_path, control_path, inbox_path
from .spawn import ensure_daemon_alive


log = logging.getLogger("core.router")
IDLE_THRESHOLD_SECS = 2 * 3600
RECENT_SELECTION_GRACE_SECS = 10 * 60


def _selected_recently(alias: str) -> bool:
    meta = sx.load_meta_for(alias) or {}
    raw = meta.get("selected_at")
    if not raw:
        return False
    try:
        selected_at = datetime.fromisoformat(str(raw)).timestamp()
    except Exception:
        return False
    return (time.time() - selected_at) <= RECENT_SELECTION_GRACE_SECS


def _is_pty_control_input(text: str, alias: str | None) -> bool:
    if not alias:
        return False
    s = text.strip()
    if not s or not re.fullmatch(r"\d+", s):
        return False
    mode_file = control_mode_path(alias)
    if not mode_file.exists():
        return False
    try:
        return mode_file.read_text(encoding="utf-8").strip() == "menu"
    except Exception:
        return False


@dataclass
class RouteOutcome:
    reply: Optional[str] = None
    routed: bool = False
    alias: Optional[str] = None


async def route_inbound(text: str, source: str) -> RouteOutcome:
    if cmd.is_command(text):
        alias_before = sx.get_current()
        reply = cmd.handle_command(text)
        alias_after = sx.get_current()
        return RouteOutcome(reply=reply, alias=alias_after or alias_before)

    text = cmd.strip_passthrough_prefix(text)

    alias = sx.get_current()
    if not alias:
        return RouteOutcome(reply="⚠️ 还没有活动会话，请先在 dashboard 创建一个。")

    if _is_pty_control_input(text, alias):
        p = control_path(alias)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(text.strip(), encoding="utf-8")
            return RouteOutcome(reply=f"已发送控制序列 {text.strip()}", alias=alias)
        except Exception as e:
            log.warning("pty control write failed for %s: %s", alias, e)
            return RouteOutcome(reply="⚠️ 控制序列发送失败，请稍后重试。", alias=alias)

    last_active = sx._last_active(alias) or 0.0
    idle_secs = time.time() - last_active if last_active else 0.0
    if last_active and idle_secs > IDLE_THRESHOLD_SECS and not _selected_recently(alias):
        hrs = int(idle_secs // 3600)
        log.info("idle-gate[%s]: idle=%.0fs, prompting /proj", alias, idle_secs)
        prompt = (
            f"⚠️ 会话 {alias!r} 已 {hrs} 小时没有动静。\n"
            "选项目继续聊或开新会话：\n\n"
            + cmd.handle_command("/proj")
        )
        return RouteOutcome(reply=prompt, alias=alias)

    alive = await ensure_daemon_alive(alias)
    if not alive:
        return RouteOutcome(reply="⚠️ agent 拉起失败，请稍后再试。", alias=alias)

    p = inbox_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(text, encoding="utf-8")
    except Exception as e:
        log.warning("inbox write failed for %s: %s", alias, e)
        return RouteOutcome(reply="⚠️ 内部错误：消息无法写入会话。", alias=alias)

    history = load_history(alias)
    history.append({"role": "user", "text": text, "ts": now_iso(), "source": source})
    save_history(history, alias)
    return RouteOutcome(routed=True, alias=alias)
