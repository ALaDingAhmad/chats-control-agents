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
from .paths import control_mode_path, control_path, inbox_path, loop_marker_fresh, outbox_path
from .pid_track import _pid_alive
from .proj_choices import proj_choices_active, write_proj_choices
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


def _consume_pty_arm(alias: str | None) -> bool:
    """One-shot: was a child-TUI menu armed? Read-and-delete the flag.

    daemon writes `control_mode_path == "menu"` only right after it relayed a
    block that looks like a selectable menu. We consume (delete) it on the very
    next inbound, so a bare digit counts as a PTY control sequence only in the
    turn immediately after the menu appeared — never lingering into chat.
    """
    if not alias:
        return False
    mode_file = control_mode_path(alias)
    if not mode_file.exists():
        return False
    try:
        armed = mode_file.read_text(encoding="utf-8").strip() == "menu"
    except Exception:
        armed = False
    try:
        mode_file.unlink()
    except Exception:
        pass
    return armed


@dataclass
class RouteOutcome:
    reply: Optional[str] = None
    routed: bool = False
    alias: Optional[str] = None


async def route_inbound(text: str, source: str) -> RouteOutcome:
    # ── one-shot menu arming, consumed THIS turn (see docs/入站路由.md) ──
    # Read+delete the pty arm and snapshot the proj arm before any dispatch,
    # so a bare digit counts as a menu pick only in the turn right after the
    # menu showed up; otherwise it falls through to chat.
    alias0 = sx.get_current()
    pty_armed = _consume_pty_arm(alias0)
    proj_armed = proj_choices_active()
    is_digit = text.strip().isdigit()

    if cmd.is_command(text):
        # digit-while-proj-armed lands here and picks the project; the pick
        # clears proj_choices internally.
        alias_before = sx.get_current()
        reply = cmd.handle_command(text)
        alias_after = sx.get_current()
        return RouteOutcome(reply=reply, alias=alias_after or alias_before)

    # Not a proj pick. If a proj menu was armed, this inbound disarms it —
    # control and chat don't mix; the menu only lived for that one turn.
    if proj_armed:
        write_proj_choices(None)

    text = cmd.strip_passthrough_prefix(text)

    alias = sx.get_current()
    if not alias:
        # 零会话：不把用户赶去 dashboard，直接弹 /proj 项目菜单让他就地建会话。
        # 复用 handle_command("/proj") 保证菜单格式/分页/arm 完全一致（见 docs/入站路由.md 第 3 条）。
        log.info("no current session — showing /proj menu for inbound")
        menu = cmd.handle_command("/proj")
        return RouteOutcome(reply=menu)

    if is_digit and pty_armed:
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

    # ── 执行端可服务性（docs/入站路由.md 决策顺序第 5 步）──
    # bridge-owned 会话（daemon 不活但 bridge 活）：不许叠 daemon；只有
    # chats-loop marker 在（循环真在收件）才投递，否则回菜单让用户显式选。
    meta = sx.load_meta_for(alias) or {}
    daemon_alive = bool(meta.get("daemon_pid")) and _pid_alive(meta.get("daemon_pid"))
    bridge_alive = bool(meta.get("bridge_pid")) and _pid_alive(meta.get("bridge_pid"))
    if not daemon_alive and bridge_alive:
        if not loop_marker_fresh(alias):
            log.info("bridge-gate[%s]: bridge alive but chats-loop inactive, prompting /proj", alias)
            prompt = (
                f"⚠️ 当前会话 {alias!r} 挂着一个终端 claude，但 chats-loop 没在跑，"
                "消息没人接。\n选一个会话/项目继续，或开新会话：\n\n"
                + cmd.handle_command("/proj")
            )
            return RouteOutcome(reply=prompt, alias=alias)
    else:
        alive = await ensure_daemon_alive(alias)
        if not alive:
            return RouteOutcome(reply="⚠️ agent 拉起失败，请稍后再试。", alias=alias)

    try:
        outbox_path(alias).write_text("", encoding="utf-8")
    except Exception:
        pass

    p = inbox_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 追加而非覆写：后端处理中时多条入站排队积压，下轮 wait 一次取走。
        # 覆写语义会让"处理期间连发两条"只活最后一条（docs/入站路由.md 第 6 步）。
        pending = ""
        if p.exists():
            try:
                pending = p.read_text(encoding="utf-8").strip()
            except Exception:
                pending = ""
        p.write_text(f"{pending}\n{text}" if pending else text, encoding="utf-8")
    except Exception as e:
        log.warning("inbox write failed for %s: %s", alias, e)
        return RouteOutcome(reply="⚠️ 内部错误：消息无法写入会话。", alias=alias)

    history = load_history(alias)
    history.append({"role": "user", "text": text, "ts": now_iso(), "source": source})
    save_history(history, alias)
    return RouteOutcome(routed=True, alias=alias)
