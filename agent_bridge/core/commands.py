"""Slash command router.

Browser and WeChat both delegate slash commands here so behaviour stays
identical across surfaces.

Conventions:
  /xxx       — bridge command, handled in this module
  //xxx      — passthrough; first slash stripped, sent to the child agent
                as a slash command (//handoff → /handoff for claude-code)
  <digit>    — only treated as a command when /proj has just been issued;
                interpreted as picking that project number

Returned strings are user-facing replies. Mutations (set_current, autospawn
queue writes, etc.) happen as side effects of _cmd_* helpers.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .autospawn import request_autospawn
from .config import get_workspace_roots
from .paths import ALIAS_RE, ROOT, meta_path, session_dir
from .pid_track import _kill_pid, _pid_alive
from .proj_choices import (
    PROJ_PICK_WINDOW_SECS,
    proj_choices_active,
    read_proj_choices,
    write_proj_choices,
)
from .projects import list_projects
from .sessions import (
    get_current,
    list_sessions,
    load_meta_for,
    save_meta_for,
    set_current,
)


# /end requires a second confirmation within this window
END_CONFIRM_WINDOW_SECS = 60
_pending_end_at: dict[str, float] = {}  # alias → first /end timestamp

# /proj output paginates this many projects per message (WeChat-friendly)
_PROJ_PAGE_SIZE = 25


# ── Dispatcher ───────────────────────────────────────────────────────────
def is_command(text: str) -> bool:
    """True if the message is a bridge command (single-/ prefix, or a bare
    integer during an active /proj pick window). `//` passthroughs return
    False here so they flow to the child agent."""
    t = text.strip()
    if t.startswith("//"):
        return False
    if t.startswith("/"):
        return True
    if t.isdigit() and proj_choices_active():
        return True
    return False


def strip_passthrough_prefix(text: str) -> str:
    """If text starts with //, drop one slash so the child agent sees /xxx."""
    t = text.lstrip()
    leading_ws = text[:len(text) - len(t)]
    if t.startswith("//"):
        return leading_ws + t[1:]
    return text


def handle_command(text: str) -> str:
    text = text.strip()
    # Bare integer in active /proj selection window → pick that project
    if text.isdigit() and proj_choices_active():
        return _cmd_pick_proj(int(text))
    if not text.startswith("/"):
        return "不是命令。命令必须以 / 开头。"
    parts = text[1:].split()
    if not parts:
        return _help_text()
    cmd, args = parts[0].lower(), parts[1:]

    if cmd in ("help", "h", "?"):
        return _help_text()
    if cmd == "list":
        return _cmd_list()
    if cmd == "proj":
        return _cmd_proj(args)
    if cmd == "use":
        if not args:
            return "用法：/use <alias>"
        return _cmd_use(args[0])
    if cmd == "new":
        if not args:
            return "用法：/new <alias> [<cwd>]"
        return _cmd_new(args[0], args[1] if len(args) > 1 else None)
    if cmd == "end":
        if not args:
            return "用法：/end <alias>（需要在 60s 内再发一次确认）"
        return _cmd_end(args[0])
    if cmd == "rename":
        if not args:
            return "用法：/rename <new-alias>"
        return _cmd_rename(args[0])
    return f"未知命令：/{cmd}\n\n" + _help_text()


def _help_text() -> str:
    # Note: WeChat for Windows desktop has a rendering bug where multi-line
    # plain text from iLink Bots collapses to one line. Mobile WeChat and
    # the browser render newlines correctly; we treat PC WeChat as known-bad.
    return (
        "可用命令：\n"
        "/proj — 列出工作空间下的项目（回复编号切换/启动）\n"
        "/list — 列出所有会话\n"
        "/use 「alias」— 切到指定会话\n"
        "/new 「alias」「cwd」— 新建会话\n"
        "/end 「alias」— 结束会话（60s 内再发一次确认）\n"
        "/rename 「new」— 重命名当前会话\n"
        "/help — 显示本帮助\n"
        "//xxx — 把 /xxx 透传给 claude（如 //handoff, //recall）"
    )


# ── /list ────────────────────────────────────────────────────────────────
def _cmd_list() -> str:
    sessions = list_sessions()
    if not sessions:
        return "没有会话。/new <alias> 创建第一个。"
    lines = ["会话列表（* = 当前选中）："]
    for s in sessions:
        prefix = "*" if s["current"] else " "
        status = "在线" if s["online"] else "离线"
        cwd_short = (s["cwd"] or "").replace(str(Path.home()), "~")
        if len(cwd_short) > 40:
            cwd_short = "…" + cwd_short[-37:]
        active = ""
        if s["last_active"]:
            mins = int((time.time() - s["last_active"]) / 60)
            if mins < 1:
                active = "刚刚"
            elif mins < 60:
                active = f"{mins}分钟前"
            elif mins < 24 * 60:
                active = f"{mins // 60}小时前"
            else:
                active = f"{mins // (24 * 60)}天前"
        lines.append(f"{prefix} {s['alias']:<12} {status}  {cwd_short:<40} {active}")
    return "\n".join(lines)


# ── /proj ────────────────────────────────────────────────────────────────
def _cmd_proj(args: list[str]) -> str:
    """List projects across all workspace_roots. Paged.

    Usage:
        /proj          page 1
        /proj 2        page N
        /proj more     next page after last shown
    """
    roots = get_workspace_roots()
    if not roots:
        return ("没有可用的工作空间。\n"
                "在 config.json 加 workspace_roots，比如：\n"
                '{"workspace_roots": ["D:/aiproject", "F:/wslshare"]}')
    projects = list_projects()
    if not projects:
        return f"工作空间 {', '.join(str(r) for r in roots)} 下没有项目目录。"

    page = 1
    if args:
        a = args[0].lower()
        if a == "more":
            last_page = (read_proj_choices() or {}).get("page", 1)
            page = last_page + 1
        elif a.isdigit():
            page = max(1, int(a))
    total = len(projects)
    pages = (total + _PROJ_PAGE_SIZE - 1) // _PROJ_PAGE_SIZE
    page = min(page, pages) if pages else 1
    start = (page - 1) * _PROJ_PAGE_SIZE
    end = min(start + _PROJ_PAGE_SIZE, total)
    visible = projects[start:end]

    lines = [f"项目（第 {page}/{pages} 页，共 {total} 个）"]
    last_root = None
    for i, p in enumerate(visible, start=start + 1):
        if p["root"] != last_root:
            lines.append(f"— {p['root']} —")
            last_root = p["root"]
        if p["online"]:
            tag = f"在线 → {p['alias']}"
        elif p["alias"]:
            tag = f"离线 → {p['alias']}"
        else:
            tag = "未运行"
        lines.append(f"{i}. {p['name']} [{tag}]")
    if page < pages:
        lines.append(f"回复 /proj more 看下一页（剩 {total - end} 个）")
    lines.append("回复编号切换；未运行的项目会自动启动 daemon。")

    write_proj_choices({
        "projects": projects,
        "page": page,
        "expires_at": time.time() + PROJ_PICK_WINDOW_SECS,
    })
    return "\n".join(lines)


def _cmd_pick_proj(n: int) -> str:
    """Pick project #n from the most recent /proj listing."""
    choices = read_proj_choices() or {}
    projects: list[dict] = choices.get("projects") or []
    if not projects:
        return "没有可选项目。先发 /proj 看列表。"
    if n < 1 or n > len(projects):
        return f"编号 {n} 越界（共 {len(projects)} 个）。再发 /proj 看列表。"
    p = projects[n - 1]

    # Already wired-up project
    if p["alias"]:
        if p["online"]:
            try:
                set_current(p["alias"])
                write_proj_choices(None)
                return f"已切到 {p['alias']}（{p['abs_path']}）。"
            except ValueError as e:
                return f"切换失败：{e}"
        try:
            set_current(p["alias"])
        except Exception:
            pass
        request_autospawn(p["alias"], p["abs_path"])
        write_proj_choices(None)
        return (
            f"已切到 {p['alias']}（{p['abs_path']}），"
            f"daemon 离线，正在自动启动（约 10 秒就绪）。"
        )

    # New project: derive alias from directory basename
    alias = p["name"]
    if not ALIAS_RE.match(alias):
        alias = re.sub(r"[^a-zA-Z0-9_\-一-鿿]", "_", alias)[:32] or "proj"
    base = alias
    n_suffix = 1
    while session_dir(alias).exists():
        m = load_meta_for(alias) or {}
        existing_cwd = (m or {}).get("cwd", "")
        try:
            if existing_cwd and str(Path(existing_cwd).resolve()).lower() == str(Path(p["abs_path"]).resolve()).lower():
                break
        except Exception:
            pass
        n_suffix += 1
        alias = f"{base}_{n_suffix}"

    sd = session_dir(alias)
    sd.mkdir(parents=True, exist_ok=True)
    save_meta_for(alias, {
        "alias": alias,
        "cwd": p["abs_path"],
        "daemon_pid": None,
        "child_pid": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    try:
        set_current(alias)
    except Exception:
        pass
    request_autospawn(alias, p["abs_path"])
    write_proj_choices(None)
    return (
        f"已切到 {alias}（{p['abs_path']}），"
        f"正在自动启动 daemon（约 10 秒就绪）。"
    )


# ── /use ─────────────────────────────────────────────────────────────────
def _cmd_use(alias: str) -> str:
    if not ALIAS_RE.match(alias):
        return f"非法 alias：{alias!r}（只能用字母数字下划线连字符或中文，1-32 字符）"
    if not session_dir(alias).exists():
        return f"会话 {alias!r} 不存在。/list 查看已有会话，或 /new {alias} 新建。"
    try:
        set_current(alias)
    except ValueError as e:
        return f"切换失败：{e}"
    m = load_meta_for(alias) or {}
    online = bool(m.get("daemon_pid")) and _pid_alive(m.get("daemon_pid"))
    note = "" if online else f"\n⚠️ 该会话离线。在电脑跑：\n  python -m agent_bridge.backends.claude_code.daemon {alias}"
    return f"已切到会话 {alias!r}。{note}"


# ── /new ─────────────────────────────────────────────────────────────────
def _cmd_new(alias: str, cwd: Optional[str]) -> str:
    if not ALIAS_RE.match(alias):
        return f"非法 alias：{alias!r}"
    sd = session_dir(alias)
    if sd.exists() and any(sd.iterdir()):
        return f"会话 {alias!r} 已存在。/use {alias} 切过去，或换个名字。"
    sd.mkdir(parents=True, exist_ok=True)
    save_meta_for(alias, {
        "alias": alias,
        "cwd": cwd or "",
        "daemon_pid": None,
        "child_pid": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    set_current(alias)
    cwd_arg = f" {cwd}" if cwd else ""
    return (
        f"已创建会话 {alias!r}，并切到它。\n"
        f"现在在电脑终端跑：\n"
        f"  python -m agent_bridge.backends.claude_code.daemon {alias}{cwd_arg}\n"
        f"启动后你发的消息会自动到这个会话。"
    )


# ── /end ─────────────────────────────────────────────────────────────────
def _cmd_end(alias: str) -> str:
    if not ALIAS_RE.match(alias):
        return f"非法 alias：{alias!r}"
    if not session_dir(alias).exists():
        return f"会话 {alias!r} 不存在。"
    now = time.time()
    pending = _pending_end_at.get(alias)
    if not pending or (now - pending) > END_CONFIRM_WINDOW_SECS:
        _pending_end_at[alias] = now
        return (
            f"确认结束 {alias!r}？60 秒内再发一次同样的 /end {alias} 真的执行。\n"
            f"（避免误杀长跑会话）"
        )
    _pending_end_at.pop(alias, None)
    m = load_meta_for(alias) or {}
    daemon_pid = m.get("daemon_pid")
    if not daemon_pid:
        return f"{alias!r} 没有活跃 daemon，已忽略。"
    if _kill_pid(daemon_pid):
        return f"已结束 {alias!r}（daemon pid={daemon_pid}）。会话目录保留。"
    return f"杀 daemon pid={daemon_pid} 失败。"


# ── /rename ──────────────────────────────────────────────────────────────
def _cmd_rename(new_alias: str) -> str:
    if not ALIAS_RE.match(new_alias):
        return f"非法 alias：{new_alias!r}"
    cur = get_current()
    if not cur:
        return "当前没有活跃会话，先 /proj 选一个项目或在 dashboard 新建。"
    if cur == new_alias:
        return f"当前会话已经叫 {new_alias!r}。"
    m = load_meta_for(cur) or {}
    if m.get("daemon_pid") and _pid_alive(m.get("daemon_pid")):
        return (
            f"当前会话 {cur!r} 在线，重命名会丢 daemon。\n"
            f"先 /end {cur}，再 /rename。"
        )
    new_dir = session_dir(new_alias)
    if new_dir.exists():
        return f"{new_alias!r} 已存在，换个名字。"
    try:
        session_dir(cur).rename(new_dir)
    except Exception as e:
        return f"重命名失败：{e}"
    m["alias"] = new_alias
    save_meta_for(new_alias, m)
    set_current(new_alias)
    return f"已重命名 {cur!r} → {new_alias!r}。"
