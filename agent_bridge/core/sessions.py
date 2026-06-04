"""
Session registry + command router.

Sessions live on disk at chat_sessions/<alias>/. This module is the single
source of truth for what counts as a session, who is currently selected,
and how `/list /use /new /end /rename /help` are processed.

Both web_server.py (browser side) and the WeChat inbound path delegate
command handling here so behaviour stays identical across surfaces.
"""
from __future__ import annotations

import json
import os
import re
import signal
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
SESSIONS_ROOT = ROOT / "chat_sessions"
SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)
CURRENT_FILE = SESSIONS_ROOT / "_current.txt"
CONFIG_FILE = ROOT / "config.json"

ALIAS_RE = re.compile(r"^[a-zA-Z0-9_\-一-鿿]{1,32}$")
DEFAULT_ALIAS = "default"

# /end requires confirmation within this many seconds
END_CONFIRM_WINDOW_SECS = 60
_pending_end_at: dict[str, float] = {}  # alias → first /end timestamp

# /proj selection window — after /proj outputs a numbered list, a bare integer
# message within this window is interpreted as picking that project.
# Persisted to disk so it survives web_server restarts (the user's /proj
# listing may have come from a previous process lifetime).
PROJ_PICK_WINDOW_SECS = 120
PROJ_CHOICES_FILE = SESSIONS_ROOT / "_pending_proj.json"


def _read_proj_choices() -> dict | None:
    if not PROJ_CHOICES_FILE.exists():
        return None
    try:
        data = json.loads(PROJ_CHOICES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if data.get("expires_at", 0) < time.time():
            try:
                PROJ_CHOICES_FILE.unlink()
            except Exception:
                pass
            return None
        return data
    except Exception:
        return None


def _write_proj_choices(choices: dict | None) -> None:
    if choices is None:
        try:
            PROJ_CHOICES_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return
    try:
        tmp = PROJ_CHOICES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(choices, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PROJ_CHOICES_FILE)
    except Exception:
        pass


# ── Config (workspaces etc.) ─────────────────────────────────────────────
DEFAULT_CONFIG = {
    "workspace_roots": ["D:/aiproject", "F:/wslshare"],
}


def load_config() -> dict:
    """Read config.json, falling back to defaults. Never raises."""
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(DEFAULT_CONFIG)
        # Fill in missing keys from defaults
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def get_workspace_roots() -> list[Path]:
    """Return validated, deduplicated, existing workspace root paths."""
    cfg = load_config()
    out: list[Path] = []
    seen: set[str] = set()
    for raw in cfg.get("workspace_roots") or []:
        try:
            p = Path(raw).resolve()
        except Exception:
            continue
        key = str(p).lower()
        if key in seen:
            continue
        if p.exists() and p.is_dir():
            out.append(p)
            seen.add(key)
    return out


def list_projects() -> list[dict]:
    """Scan every workspace root for non-hidden subdirectories. Cross-reference
    with chat_sessions/<alias>/meta.json to mark which are already wired up.

    Returns: [{root, name, abs_path, alias, online, meta_exists}, ...]
    Sorted by (root index, name).
    """
    # Build a map of existing alias → meta to know which projects already wired
    alias_by_cwd: dict[str, dict] = {}  # absolute path (lowercased) → {alias, online, ...}
    if SESSIONS_ROOT.exists():
        for entry in SESSIONS_ROOT.iterdir():
            if not entry.is_dir() or not ALIAS_RE.match(entry.name):
                continue
            mp = entry / "meta.json"
            if not mp.exists():
                continue
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                continue
            cwd_raw = (m or {}).get("cwd")
            if not cwd_raw:
                continue
            try:
                key = str(Path(cwd_raw).resolve()).lower()
            except Exception:
                continue
            daemon_pid = m.get("daemon_pid")
            online = bool(daemon_pid) and _pid_alive(daemon_pid)
            alias_by_cwd[key] = {
                "alias": entry.name,
                "online": online,
                "daemon_pid": daemon_pid if online else None,
            }

    out: list[dict] = []
    for root in get_workspace_roots():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            if child.name.startswith(".") or child.name.startswith("_"):
                continue
            try:
                key = str(child.resolve()).lower()
            except Exception:
                key = str(child).lower()
            info = alias_by_cwd.get(key)
            out.append({
                "root": str(root),
                "name": child.name,
                "abs_path": str(child),
                "alias": (info or {}).get("alias"),
                "online": (info or {}).get("online", False),
                "daemon_pid": (info or {}).get("daemon_pid"),
            })
    # Sort: online first, then has-alias-but-offline, then untouched.
    # Within each band: preserve workspace grouping, then name.
    def _sort_key(p):
        band = 0 if p["online"] else (1 if p["alias"] else 2)
        return (band, p["root"].lower(), p["name"].lower())
    out.sort(key=_sort_key)
    return out


# ── Path helpers ─────────────────────────────────────────────────────────
def session_dir(alias: str) -> Path:
    return SESSIONS_ROOT / alias


def inbox_path(alias: str) -> Path:
    return session_dir(alias) / "inbox.txt"


def outbox_path(alias: str) -> Path:
    return session_dir(alias) / "outbox.txt"


def history_path(alias: str) -> Path:
    return session_dir(alias) / "history.json"


def meta_path(alias: str) -> Path:
    return session_dir(alias) / "meta.json"


# ── Current selection ────────────────────────────────────────────────────
def get_current() -> str:
    if CURRENT_FILE.exists():
        try:
            cur = CURRENT_FILE.read_text(encoding="utf-8").strip()
            if cur and ALIAS_RE.match(cur):
                return cur
        except Exception:
            pass
    return DEFAULT_ALIAS


def set_current(alias: str) -> None:
    if not ALIAS_RE.match(alias):
        raise ValueError(f"invalid alias: {alias!r}")
    session_dir(alias).mkdir(parents=True, exist_ok=True)
    CURRENT_FILE.write_text(alias, encoding="utf-8")


# ── Session listing / liveness ───────────────────────────────────────────
def list_sessions() -> list[dict]:
    """Scan chat_sessions/ for all aliases. Returns sorted list of:
    {alias, cwd, online, daemon_pid, last_active, last_exit_at, current}
    """
    cur = get_current()
    out = []
    for entry in SESSIONS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        alias = entry.name
        if not ALIAS_RE.match(alias):
            continue
        m = _load_meta_for(alias) or {}
        daemon_pid = m.get("daemon_pid")
        online = bool(daemon_pid) and _pid_alive(daemon_pid)
        out.append({
            "alias": alias,
            "cwd": m.get("cwd", ""),
            "online": online,
            "daemon_pid": daemon_pid if online else None,
            "created_at": m.get("created_at"),
            "last_exit_at": m.get("last_exit_at"),
            "last_active": _last_active(alias),
            "current": alias == cur,
        })
    # Sort: online first, then by last_active desc
    out.sort(key=lambda s: (not s["online"], -(s["last_active"] or 0)))
    return out


def _load_meta_for(alias: str) -> Optional[dict]:
    p = meta_path(alias)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _last_active(alias: str) -> Optional[float]:
    """Approx last activity = newer of inbox/outbox/history mtime."""
    latest = 0.0
    for fn in (inbox_path, outbox_path, history_path):
        p = fn(alias)
        if p.exists():
            try:
                latest = max(latest, p.stat().st_mtime)
            except Exception:
                pass
    return latest or None


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        # Windows: os.kill(pid, 0) is unreliable — for unknown/dead PIDs Python
        # raises SystemError instead of OSError. Use OpenProcess via ctypes for
        # a cleaner check.
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                # STILL_ACTIVE = 259; if GetExitCodeProcess says anything else, dead
                exit_code = ctypes.c_ulong(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _kill_pid(pid: int) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            # Windows: SIGTERM is treated like SIGINT but os.kill works on PID
            import subprocess
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                          capture_output=True, timeout=5)
            return True
        else:
            os.kill(pid, signal.SIGTERM)
            return True
    except Exception:
        return False


# ── Command processor ────────────────────────────────────────────────────
def is_command(text: str) -> bool:
    t = text.strip()
    # `//` prefix is the escape: passes through to child claude (its own
    # slash commands like //handoff //recall //init etc.), bridge ignores it.
    if t.startswith("//"):
        return False
    if t.startswith("/"):
        return True
    # Bare integer right after /proj listing is treated as "pick that project"
    if t.isdigit() and _proj_choices_active():
        return True
    return False


def strip_passthrough_prefix(text: str) -> str:
    """If text starts with //, strip the first slash so child claude sees /cmd.
    Used by send_message / weixin inbound after is_command() returns False."""
    t = text.lstrip()
    leading_ws = text[:len(text) - len(t)]
    if t.startswith("//"):
        return leading_ws + t[1:]
    return text


def _proj_choices_active() -> bool:
    return _read_proj_choices() is not None


def handle_command(text: str) -> str:
    """Process a slash command and return a human-readable response string.
    Mutates session state (current selection, kills daemons, etc.) as needed.
    """
    text = text.strip()
    # Bare integer in active /proj selection window → pick that project
    if text.isdigit() and _proj_choices_active():
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
    # plain text from iLink Bots collapses to one line. Mobile WeChat (iOS/
    # Android) and the browser UI both render newlines correctly, so we keep
    # \n here and treat PC WeChat as a known-broken client.
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
            elif mins < 24*60:
                active = f"{mins//60}小时前"
            else:
                active = f"{mins//(24*60)}天前"
        lines.append(f"{prefix} {s['alias']:<12} {status}  {cwd_short:<40} {active}")
    return "\n".join(lines)


_PROJ_PAGE_SIZE = 25  # Keep output short enough for WeChat to render cleanly


def _cmd_proj(args: list[str]) -> str:
    """List projects across all workspace_roots. Paged.
    Usage: /proj           (page 1)
           /proj 2         (page N)
           /proj more      (next page after most recent)
    Stores the listing to disk so a bare integer reply within
    PROJ_PICK_WINDOW_SECS picks one — survives web_server restarts.
    """
    roots = get_workspace_roots()
    if not roots:
        return ("没有可用的工作空间。\n"
                "在 config.json 加 workspace_roots，比如：\n"
                '{"workspace_roots": ["D:/aiproject", "F:/wslshare"]}')
    projects = list_projects()  # already sorted: online → has-alias → name
    if not projects:
        return f"工作空间 {', '.join(str(r) for r in roots)} 下没有项目目录。"

    # Pagination
    page = 1
    if args:
        a = args[0].lower()
        if a == "more":
            last_page = (_read_proj_choices() or {}).get("page", 1)
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

    # Store the FULL projects list (not just visible) so picking works across
    # pages without needing the user to be on the same page.
    _write_proj_choices({
        "projects": projects,
        "page": page,
        "expires_at": time.time() + PROJ_PICK_WINDOW_SECS,
    })
    return "\n".join(lines)


def _cmd_pick_proj(n: int) -> str:
    """Pick project #n from the most recent /proj listing.
    Returns text describing what happened. If the project has no daemon yet,
    a sentinel marker is written so web_server.py can auto-spawn it.
    """
    choices = _read_proj_choices() or {}
    projects: list[dict] = choices.get("projects") or []
    if not projects:
        return "没有可选项目。先发 /proj 看列表。"
    if n < 1 or n > len(projects):
        return f"编号 {n} 越界（共 {len(projects)} 个）。再发 /proj 看列表。"
    p = projects[n - 1]

    # Already wired up: just switch current alias
    if p["alias"]:
        if p["online"]:
            try:
                set_current(p["alias"])
                _write_proj_choices(None)
                return f"已切到 {p['alias']}（{p['abs_path']}）。"
            except ValueError as e:
                return f"切换失败：{e}"
        else:
            # Offline alias — flag for auto-spawn under the existing alias name.
            try:
                set_current(p["alias"])
            except Exception:
                pass
            _request_autospawn(p["alias"], p["abs_path"])
            _write_proj_choices(None)
            return (
                f"已切到 {p['alias']}（{p['abs_path']}），"
                f"daemon 离线，正在自动启动（约 10 秒就绪）。"
            )

    # New project: derive alias from directory basename
    alias = p["name"]
    if not ALIAS_RE.match(alias):
        # Fallback: replace illegal chars with _
        alias = re.sub(r"[^a-zA-Z0-9_\-一-鿿]", "_", alias)[:32] or "proj"
    # If that alias collides with an existing session at a different cwd,
    # disambiguate with a numeric suffix.
    base = alias
    n_suffix = 1
    while session_dir(alias).exists():
        m = _load_meta_for(alias) or {}
        existing_cwd = (m or {}).get("cwd", "")
        try:
            if existing_cwd and str(Path(existing_cwd).resolve()).lower() == str(Path(p["abs_path"]).resolve()).lower():
                # Same project, same alias — should have been caught above, but
                # treat as match.
                break
        except Exception:
            pass
        n_suffix += 1
        alias = f"{base}_{n_suffix}"

    # Pre-create the session dir + meta so /list sees it immediately
    sd = session_dir(alias)
    sd.mkdir(parents=True, exist_ok=True)
    meta = {
        "alias": alias,
        "cwd": p["abs_path"],
        "daemon_pid": None,
        "child_pid": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path(alias).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        set_current(alias)
    except Exception:
        pass

    _request_autospawn(alias, p["abs_path"])
    _write_proj_choices(None)
    return (
        f"已切到 {alias}（{p['abs_path']}），"
        f"正在自动启动 daemon（约 10 秒就绪）。"
    )


def _request_autospawn(alias: str, cwd: str) -> None:
    """Write a sentinel file that web_server polls and acts on. Decouples
    sessions.py (pure logic) from process spawning (web_server.py)."""
    p = SESSIONS_ROOT / "_autospawn_queue.jsonl"
    rec = {
        "alias": alias,
        "cwd": cwd,
        "requested_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _cmd_use(alias: str) -> str:
    if not ALIAS_RE.match(alias):
        return f"非法 alias：{alias!r}（只能用字母数字下划线连字符或中文，1-32 字符）"
    if not session_dir(alias).exists():
        return f"会话 {alias!r} 不存在。/list 查看已有会话，或 /new {alias} 新建。"
    try:
        set_current(alias)
    except ValueError as e:
        return f"切换失败：{e}"
    m = _load_meta_for(alias) or {}
    online = bool(m.get("daemon_pid")) and _pid_alive(m.get("daemon_pid"))
    note = "" if online else f"\n⚠️ 该会话离线。在电脑跑：\n  python claude_daemon.py {alias}"
    return f"已切到会话 {alias!r}。{note}"


def _cmd_new(alias: str, cwd: Optional[str]) -> str:
    if not ALIAS_RE.match(alias):
        return f"非法 alias：{alias!r}"
    sd = session_dir(alias)
    if sd.exists() and any(sd.iterdir()):
        return f"会话 {alias!r} 已存在。/use {alias} 切过去，或换个名字。"
    sd.mkdir(parents=True, exist_ok=True)
    # Pre-seed meta so /list 看得到
    meta = {
        "alias": alias,
        "cwd": cwd or "",
        "daemon_pid": None,
        "child_pid": None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path(alias).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    set_current(alias)
    cwd_arg = f" {cwd}" if cwd else ""
    return (
        f"已创建会话 {alias!r}，并切到它。\n"
        f"现在在电脑终端跑：\n"
        f"  python D:/aiproject/claude-mcp-bridge/claude_daemon.py {alias}{cwd_arg}\n"
        f"启动后你发的消息会自动到这个会话。"
    )


def _cmd_end(alias: str) -> str:
    if not ALIAS_RE.match(alias):
        return f"非法 alias：{alias!r}"
    if not session_dir(alias).exists():
        return f"会话 {alias!r} 不存在。"
    now = time.time()
    last = _pending_end_at.get(alias)
    if last is None or (now - last) > END_CONFIRM_WINDOW_SECS:
        _pending_end_at[alias] = now
        return (
            f"⚠️ 即将结束会话 {alias!r}（杀掉 daemon，会话历史保留）。\n"
            f"60 秒内再发一次 `/end {alias}` 确认。"
        )
    # Confirmed — actually kill
    _pending_end_at.pop(alias, None)
    m = _load_meta_for(alias) or {}
    pid = m.get("daemon_pid")
    if pid and _pid_alive(pid):
        ok = _kill_pid(pid)
        if not ok:
            return f"杀 daemon (pid={pid}) 失败，请手动处理。"
        return f"已结束会话 {alias!r}（daemon pid={pid}）。历史保留。"
    return f"会话 {alias!r} 本就离线，未做任何动作。"


def _cmd_rename(new_alias: str) -> str:
    if not ALIAS_RE.match(new_alias):
        return f"非法 alias：{new_alias!r}"
    cur = get_current()
    if new_alias == cur:
        return f"当前会话已经叫 {new_alias!r}。"
    if session_dir(new_alias).exists():
        return f"{new_alias!r} 已被占用。换个名字。"
    # Note: renaming a running session is risky — its mcp_bridge still uses old alias
    # in env. Refuse if online.
    m = _load_meta_for(cur) or {}
    if m.get("daemon_pid") and _pid_alive(m["daemon_pid"]):
        return (
            f"⚠️ 会话 {cur!r} 当前在线（daemon pid={m['daemon_pid']}）。"
            f"重命名会让正在跑的 mcp_bridge 找不到 inbox。\n"
            f"请先 /end {cur} 结束它，重启后再 /rename。"
        )
    # Safe to rename
    session_dir(cur).rename(session_dir(new_alias))
    set_current(new_alias)
    # Patch meta
    m2 = _load_meta_for(new_alias) or {}
    m2["alias"] = new_alias
    meta_path(new_alias).write_text(json.dumps(m2, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"会话已重命名：{cur!r} → {new_alias!r}"


# ── Migration: move legacy single-session files into chat_sessions/default/ ──
def migrate_legacy_if_present() -> None:
    """One-shot: if old chat_inbox.txt etc. exist at project root, move them
    into chat_sessions/default/. Safe to call on every startup.
    """
    sd = session_dir(DEFAULT_ALIAS)
    sd.mkdir(parents=True, exist_ok=True)
    moves = [
        ("chat_inbox.txt", "inbox.txt"),
        ("chat_outbox.txt", "outbox.txt"),
        ("chat_history.json", "history.json"),
    ]
    for old_name, new_name in moves:
        old = ROOT / old_name
        new = sd / new_name
        if old.exists() and not new.exists():
            try:
                old.rename(new)
            except Exception:
                pass
        elif old.exists() and new.exists():
            # Both exist — keep new, archive old
            try:
                old.rename(ROOT / (old_name + ".legacy"))
            except Exception:
                pass


# ── Daemon-child PID tracking (cross-platform) ───────────────────────────
# Whenever claude_daemon.py spawns its child claude.exe, it appends a record
# to chat_sessions/<alias>/spawned_pids.jsonl with {pid, create_time, ...}.
# Cleanup tooling reads these files to safely distinguish:
#   - daemon-spawned children (anything matching a logged pid + create_time)
#   - PID-recycled strangers (pid matches but create_time differs)
#   - user-launched claude.exe (never logged)
# This is the key invariant that lets _kill_daemon_children.py kill orphans
# without hitting the user's own interactive claude sessions.

def _spawned_log_path(alias: str) -> Path:
    return session_dir(alias) / "spawned_pids.jsonl"


def list_logged_child_records() -> list[dict]:
    """Read every spawned_pids.jsonl across all aliases and return raw records.
    Records may include dead PIDs — caller decides what to do with those.
    """
    out: list[dict] = []
    if not SESSIONS_ROOT.exists():
        return out
    for entry in SESSIONS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        alias = entry.name
        if not ALIAS_RE.match(alias):
            continue
        p = _spawned_log_path(alias)
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    rec["_alias"] = alias
                    out.append(rec)
                except Exception:
                    continue
        except Exception:
            continue
    return out


def list_daemon_child_pids() -> set[int]:
    """Return PIDs that are (a) still alive and (b) whose create_time matches
    the value we logged when daemon spawned them. PID recycling is rejected
    via the create_time check (tolerance 1.0 second to absorb FS timestamp
    rounding).
    """
    try:
        import psutil
    except ImportError:
        # No psutil → fall back to "any logged pid that responds to OpenProcess"
        # i.e. trust pid-only. Less safe but better than nothing.
        return {rec["pid"] for rec in list_logged_child_records()
                if rec.get("pid") and _pid_alive(rec["pid"])}

    result: set[int] = set()
    for rec in list_logged_child_records():
        pid = rec.get("pid")
        if not pid:
            continue
        logged_ct = rec.get("create_time")
        try:
            proc = psutil.Process(pid)
            if logged_ct is None:
                # Older records without create_time: trust pid only.
                result.add(pid)
                continue
            actual_ct = proc.create_time()
            if abs(actual_ct - logged_ct) < 1.0:
                result.add(pid)
            # else: PID was recycled, skip
        except psutil.NoSuchProcess:
            continue
        except Exception:
            continue
    return result


def is_daemon_child(pid: int) -> bool:
    return pid in list_daemon_child_pids()


def list_daemon_descendants() -> set[int]:
    """Daemon child PIDs plus all of their descendants (mcp_bridge subprocesses,
    cmd.exe shims, etc.). Used by the cleanup script to take down the whole
    tree, not just the child claude.exe.
    """
    try:
        import psutil
    except ImportError:
        return list_daemon_child_pids()
    roots = list_daemon_child_pids()
    out: set[int] = set(roots)
    for pid in roots:
        try:
            for child in psutil.Process(pid).children(recursive=True):
                out.add(child.pid)
        except Exception:
            continue
    return out
