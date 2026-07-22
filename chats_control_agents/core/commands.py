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

import re
import time
from pathlib import Path

from .autospawn import request_autospawn
from .config import get_workspace_roots
from .paths import ALIAS_RE, control_path, session_dir
from .pid_track import _kill_pid, _pid_alive
from .proj_choices import (
    PROJ_PICK_WINDOW_SECS,
    proj_choices_active,
    read_proj_choices,
    write_proj_choices,
)
from .projects import list_projects
from .resume_choices import (
    RESUME_PICK_WINDOW_SECS,
    read_resume_choices,
    resume_choices_active,
    write_resume_choices,
)
from .resume_scan import list_recent_sessions
from .sessions import (
    KNOWN_BACKENDS,
    create_session_dir,
    get_current,
    get_default_backend,
    list_sessions,
    load_meta_for,
    save_meta_for,
    set_current,
    set_default_backend,
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
    if t.isdigit() and (resume_choices_active() or proj_choices_active()):
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
    # Bare integer: second-level resume session pick takes priority over the
    # first-level /proj project pick (the resume menu is armed *after* a project
    # was chosen, so if both are somehow active the newer one wins).
    if text.isdigit() and resume_choices_active():
        return _cmd_pick_resume(int(text))
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
        return _cmd_proj([], for_new=True)
    if cmd == "end":
        if not args:
            return "用法：/end <alias>（需要在 60s 内再发一次确认）"
        return _cmd_end(args[0])
    if cmd == "stop":
        return _cmd_stop()
    if cmd == "rename":
        if not args:
            return "用法：/rename <new-alias>"
        return _cmd_rename(args[0])
    if cmd == "backend":
        return _cmd_backend(args)
    return f"未知命令：/{cmd}\n\n" + _help_text()


def _help_text() -> str:
    # Note: WeChat for Windows desktop has a rendering bug where multi-line
    # plain text from iLink Bots collapses to one line. Mobile WeChat and
    # the browser render newlines correctly; we treat PC WeChat as known-bad.
    return (
        "可用命令：\n"
        "/proj [关键词] — 列项目→选项目→接回该项目历史会话（默认恢复上下文）\n"
        "/list — 列出所有会话\n"
        "/use 「alias」— 切到指定会话\n"
        "/new — 列项目→开全新会话（不接回历史；回 0 开空会话）\n"
        "/end 「alias」— 结束会话（60s 内再发一次确认）\n"
        "/stop — 中断当前会话正在执行的任务（发 ESC）\n"
        "/rename 「new」— 重命名当前会话\n"
        "/backend — 看/切默认 AI 后端（用于之后新建的会话）\n"
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
def _cmd_proj(args: list[str], *, for_new: bool = False) -> str:
    """List projects across all workspace_roots. Paged.

    Usage:
        /proj          page 1
        /proj 2        page N
        /proj more     next page after last shown

    for_new: set by /new. When True, picking a project skips the resume menu
    and starts a fresh session (see _cmd_pick_proj). /proj (for_new=False) is
    the resume-by-default path.
    """
    roots = get_workspace_roots()
    if not roots:
        return ("没有可用的工作空间。\n"
                "在 config.json 加 workspace_roots，比如：\n"
                '{"workspace_roots": ["/path/to/your/projects"]}')
    projects = list_projects()
    if not projects:
        return f"工作空间 {', '.join(str(r) for r in roots)} 下没有项目目录。"

    page = 1
    search_term = ""
    if args:
        a = args[0].lower()
        if a == "more":
            prev = read_proj_choices() or {}
            page = prev.get("page", 1) + 1
            # 分页重入：继承上一次的 for_new，别让翻页把 /new 语义丢了
            for_new = for_new or bool(prev.get("for_new"))
        elif a.isdigit():
            page = max(1, int(a))
        else:
            search_term = a
            projects = [p for p in projects if search_term in p["name"].lower()]
            if not projects:
                return f"没有匹配 '{search_term}' 的项目。"

    total = len(projects)
    pages = (total + _PROJ_PAGE_SIZE - 1) // _PROJ_PAGE_SIZE
    page = min(page, pages) if pages else 1
    start = (page - 1) * _PROJ_PAGE_SIZE
    end = min(start + _PROJ_PAGE_SIZE, total)
    visible = projects[start:end]

    header = f"项目（第 {page}/{pages} 页，共 {total} 个）"
    if search_term:
        header += f"  搜索：{search_term}"
    lines = [header]
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
    if for_new:
        lines.append("回复编号开全新会话（不接回历史）；未运行的会自动启动 daemon。")
        lines.append("回复 0 开空会话（cwd=用户主目录，不绑任何项目）。")
    else:
        lines.append("回复编号 → 选该项目要接回的历史会话。")
        lines.append("回复 0 开空会话（主目录）。想开全新会话用 /new。")

    write_proj_choices({
        "projects": projects,
        "page": page,
        "for_new": for_new,
        "expires_at": time.time() + PROJ_PICK_WINDOW_SECS,
    })
    return "\n".join(lines)


def _cmd_pick_proj(n: int) -> str:
    """Pick project #n from the most recent /proj listing.

    Semantics (2026-07-21, resume 默认化): picking a project no longer starts a
    session directly. If the default backend is claude_channel AND the project's
    cwd has claude transcript history, we arm the second-level resume menu (see
    _enter_resume_menu). Otherwise we fall back to the old behaviour: start /
    switch a session straight away (_start_session_for_cwd).

    n == 0 is a special-case "blank session": cwd = user's home directory, no
    project association.
    """
    choices = read_proj_choices() or {}
    projects: list[dict] = choices.get("projects") or []
    for_new = bool(choices.get("for_new"))
    if not projects:
        return "没有可选项目。先发 /proj 看列表。"

    if n == 0:
        home_cwd = str(Path.home())
        write_proj_choices(None)
        return _resume_or_start(home_cwd, blank=True, for_new=for_new)

    if n < 1 or n > len(projects):
        return f"编号 {n} 越界（共 {len(projects)} 个）。再发 /proj 看列表。"
    p = projects[n - 1]
    write_proj_choices(None)
    return _resume_or_start(p["abs_path"], project=p, for_new=for_new)


def _resume_or_start(cwd: str, *, project: dict | None = None, blank: bool = False,
                     for_new: bool = False) -> str:
    """Route a chosen project cwd to either the resume menu or a fresh start.

    Resume is only wired for claude_channel (only its daemon understands the
    RESUME: control signal — see docs/后端设计.md). Skip the resume menu when:
      - for_new (the pick came from /new — user explicitly wants a fresh start), or
      - the default backend isn't claude_channel, or
      - the cwd has no transcript history.
    """
    if not for_new and get_default_backend() == "claude_channel":
        sessions = list_recent_sessions(cwd)
        if sessions:
            return _enter_resume_menu(cwd, sessions, project=project, blank=blank)
    # No resume path → start/switch a session straight away (old behaviour).
    return _start_session_for_cwd(cwd, project=project, blank=blank)


def _enter_resume_menu(cwd: str, sessions: list[dict], *, project: dict | None, blank: bool) -> str:
    """Arm the second-level resume menu and render it for the phone.

    Each row = time + a summary of the session's last few user inputs (from
    ~/.claude/history.jsonl — see resume_scan.list_recent_sessions). Replying
    with a bare integer picks a session to --resume (handled by _cmd_pick_resume).
    """
    label = "空会话（主目录）" if blank else (project or {}).get("name", cwd)
    lines = [f"「{label}」最近的会话（回复编号接回上下文）："]
    for i, s in enumerate(sessions, 1):
        ts = time.strftime("%m-%d %H:%M", time.localtime(s["mtime"]))
        lines.append(f"{i}. {ts} · {s['summary']}")
    lines.append("回复 0 开全新会话（不接回历史）。")

    write_resume_choices({
        "cwd": cwd,
        "sessions": sessions,
        "project": project,
        "blank": blank,
        "expires_at": time.time() + RESUME_PICK_WINDOW_SECS,
    })
    return "\n".join(lines)


def _cmd_pick_resume(n: int) -> str:
    """Pick session #n from the resume menu → --resume into it.

    n == 0 means "skip resume, start a fresh session" (the menu's escape hatch).
    """
    choices = read_resume_choices() or {}
    sessions: list[dict] = choices.get("sessions") or []
    cwd: str = choices.get("cwd") or str(Path.home())
    project = choices.get("project")
    blank = bool(choices.get("blank"))
    if not sessions:
        return "没有可选会话。先发 /proj 选项目。"

    if n == 0:
        write_resume_choices(None)
        return _start_session_for_cwd(cwd, project=project, blank=blank)

    if n < 1 or n > len(sessions):
        return f"编号 {n} 越界（共 {len(sessions)} 个）。再发 /proj 重来。"
    sess = sessions[n - 1]
    session_id = sess["session_id"]
    write_resume_choices(None)

    # Start/switch a claude_channel session for this cwd, then hand the daemon
    # the RESUME: control signal so it kills the fresh child and re-spawns with
    # --resume <session_id> (see docs/后端设计.md "resume 控制通路").
    reply = _start_session_for_cwd(cwd, project=project, blank=blank, silent=True)
    alias = get_current() or ""
    if not alias:
        return "起会话失败，无法接回。再发 /proj 重来。"
    try:
        control_path(alias).write_text(f"RESUME:{session_id}", encoding="utf-8")
    except Exception as e:
        return f"接回信号写入失败：{e}"
    ts = time.strftime("%m-%d %H:%M", time.localtime(sess["mtime"]))
    return (
        f"正在接回会话（{ts} · {sess['summary']}）到 {alias}，"
        f"约 10 秒后可继续对话。"
    )


def _start_session_for_cwd(cwd: str, *, project: dict | None = None,
                           blank: bool = False, silent: bool = False) -> str:
    """Create/switch a session for a cwd and autospawn its daemon.

    Extracted from the old _cmd_pick_proj body — the three original branches
    (blank session / existing-alias project / new project) collapse here.
    `silent=True` suppresses the user-facing reply (used by the resume flow,
    which appends its own "接回中" message).
    """
    from .sessions import make_alias_for_cwd

    if blank:
        alias = make_alias_for_cwd(cwd)
        create_session_dir(alias, cwd, backend=get_default_backend())
        try:
            set_current(alias)
        except Exception:
            pass
        request_autospawn(alias, cwd)
        if silent:
            return ""
        return (
            f"已开空会话 {alias}（cwd={cwd}），"
            f"正在自动启动 daemon（约 10 秒就绪）。"
        )

    # Already wired-up project (has an alias)
    if project and project.get("alias"):
        alias = project["alias"]
        if project.get("online"):
            try:
                set_current(alias)
            except ValueError as e:
                return f"切换失败：{e}"
            if silent:
                return ""
            return f"已切到 {alias}（{project['abs_path']}）。"
        try:
            set_current(alias)
        except Exception:
            pass
        request_autospawn(alias, project["abs_path"])
        if silent:
            return ""
        return (
            f"已切到 {alias}（{project['abs_path']}），"
            f"daemon 离线，正在自动启动（约 10 秒就绪）。"
        )

    # New project (or blank=False but no wired alias): derive alias from basename
    base_name = (project or {}).get("name") or Path(cwd).name or "proj"
    alias = base_name
    if not ALIAS_RE.match(alias):
        alias = re.sub(r"[^a-zA-Z0-9_\-一-鿿]", "_", alias)[:32] or "proj"
    base = alias
    n_suffix = 1
    while session_dir(alias).exists():
        m = load_meta_for(alias) or {}
        existing_cwd = (m or {}).get("cwd", "")
        try:
            if existing_cwd and str(Path(existing_cwd).resolve()).lower() == str(Path(cwd).resolve()).lower():
                break
        except Exception:
            pass
        n_suffix += 1
        alias = f"{base}_{n_suffix}"

    create_session_dir(alias, cwd, backend=get_default_backend())
    try:
        set_current(alias)
    except Exception:
        pass
    request_autospawn(alias, cwd)
    if silent:
        return ""
    return (
        f"已切到 {alias}（{cwd}），"
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
    note = "" if online else f"\n⚠️ 该会话离线。在电脑跑：\n  python -m chats_control_agents.backends.claude_code.daemon {alias}"
    return f"已切到会话 {alias!r}。{note}"


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


# ── /stop ────────────────────────────────────────────────────────────────
def _cmd_stop() -> str:
    """中断当前会话正在执行的任务。

    daemon-managed 会话：写 control_path = "9"，daemon 既有逻辑发 ESC 给
    child claude（docs/入站路由.md "/stop 命令随时可用"段）。显式命令不受
    裸数字的 one-shot arm 限制。bridge-owned（用户自己终端里的 chats-loop）
    远程中断不了——模型推理无法从信箱层抢占，只能在那个终端按 ESC。
    """
    alias = get_current()
    if not alias:
        return "当前没有活跃会话，没有可中断的任务。"
    m = load_meta_for(alias) or {}
    daemon_pid = m.get("daemon_pid")
    if daemon_pid and _pid_alive(daemon_pid):
        p = control_path(alias)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("9", encoding="utf-8")
        except Exception as e:
            return f"⚠️ 中断信号写入失败：{e}"
        return (
            f"已向会话 {alias!r} 发送中断（ESC），daemon 执行后会回执 sent ESC。\n"
            "任务停止后发下一条消息继续。"
        )
    bridge_pid = m.get("bridge_pid")
    if bridge_pid and _pid_alive(bridge_pid):
        return (
            f"会话 {alias!r} 跑在你本机的终端 claude 里（chats-loop），"
            "远程中断不了——到那个终端窗口按 ESC。"
        )
    return f"会话 {alias!r} 不在线，没有正在执行的任务。"


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


# ── /backend ─────────────────────────────────────────────────────────────
def _cmd_backend(args: list[str]) -> str:
    """看/切默认 backend——只影响之后新建的会话，已有会话不变。

    无参 → 显示当前默认 + 可选项
    有参 → 切换并落盘 _default_backend.txt
    """
    cur = get_default_backend()
    if not args:
        lines = [f"当前默认 backend：{cur}", "可选："]
        for name in KNOWN_BACKENDS:
            mark = "·" if name == cur else " "
            lines.append(f" {mark} {name}")
        lines.append("用 /backend <名字> 切换。只影响之后新建的会话。")
        return "\n".join(lines)

    target = args[0].strip().lower()
    if target == cur:
        return f"默认 backend 已经是 {target}，没改动。"
    try:
        set_default_backend(target)
    except ValueError as e:
        return f"切换失败：{e}\n可选：{', '.join(KNOWN_BACKENDS)}"
    return (
        f"已切默认 backend：{cur} → {target}。\n"
        f"之后用 /new 或 /proj 新建的会话都会用 {target}。"
    )
