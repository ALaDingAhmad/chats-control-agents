"""Workspace project enumeration.

For each configured workspace_root, list non-hidden subdirectories as
candidate projects. Cross-reference with chat_sessions/<alias>/meta.json
to mark which projects already have a wired-up session (online / offline).

Used by:
  - /proj command (commands.py)
  - browser dashboard (web/routes/projects.py)
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .config import get_workspace_roots
from .paths import ALIAS_RE, SESSIONS_ROOT
from .pid_track import _pid_alive

# A session's shell is garbage-collected from chat_sessions/ once the shell
# itself (its newest file's mtime) has been idle longer than this AND the
# session has no live process. Shell-granularity — see docs/入站路由.md
# "/proj 第一级菜单：四态显示".
OFFLINE_GC_DAYS = 3
_OFFLINE_GC_SECS = OFFLINE_GC_DAYS * 86400


def _shell_mtime(alias: str) -> float | None:
    """Newest mtime among files in chat_sessions/<alias>/ (= last activity).

    None if the shell dir is missing or empty. Reflects this specific session's
    activity, not the whole project's — so an active sibling session in the same
    cwd won't keep this one's dead shell alive.
    """
    d = SESSIONS_ROOT / alias
    if not d.is_dir():
        return None
    try:
        mtimes = [p.stat().st_mtime for p in d.iterdir() if p.is_file()]
    except Exception:
        return None
    return max(mtimes) if mtimes else None


def list_projects() -> list[dict]:
    """Scan every workspace_root for non-hidden subdirectories.

    Returns a list of dicts:
        {root, name, abs_path, alias, online, daemon_pid}

    Sorted: online → has-alias-but-offline → untouched, then by workspace
    grouping, then name.
    """
    alias_by_cwd: dict[str, dict] = {}
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
            child_pid = m.get("child_pid")
            # online = daemon 活 && child 活（僵尸 daemon 空转 child 死 → 判离线，
            # 触发重选自愈）。见 docs/入站路由.md "在线判据"。
            online = (bool(daemon_pid) and _pid_alive(daemon_pid)
                      and bool(child_pid) and _pid_alive(child_pid))
            # 同 cwd 多个 session：在线者优先占坑，不被后扫到的离线者覆盖
            prev = alias_by_cwd.get(key)
            if prev and prev["online"] and not online:
                continue
            alias_by_cwd[key] = {
                "alias": entry.name,
                "online": online,
                "daemon_pid": daemon_pid if online else None,
            }

    # 保护：绝不清正在用的当前会话（读 _current，避开 sessions 循环 import）
    cur_alias = ""
    try:
        from .paths import CURRENT_FILE
        if CURRENT_FILE.exists():
            cur_alias = CURRENT_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        cur_alias = ""

    now = time.time()
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
            info = alias_by_cwd.get(key) or {}
            alias = info.get("alias")
            online = info.get("online", False)

            # 顺带清壳：离线（有 alias、daemon 不活）且这个会话壳 3 天没动静 → 删桥
            # 这边的会话壳 chat_sessions/<alias>/。会话粒度（用壳自己最新 mtime，
            # 不是整个 cwd 的 transcript——否则同项目一个会话活跃、全项目死壳都清
            # 不掉）。绝不碰 transcript。保护：活会话、当前会话不删。见 docs/入站路由.md。
            if alias and not online and alias != cur_alias:
                sh_mtime = _shell_mtime(alias)
                stale = (sh_mtime is None) or (now - sh_mtime > _OFFLINE_GC_SECS)
                if stale:
                    try:
                        shutil.rmtree(SESSIONS_ROOT / alias, ignore_errors=True)
                    except Exception:
                        pass
                    alias = None  # 降级为"未运行"

            # 三态：daemon 活=在线；有 alias 但离线=离线；无 alias=未运行
            if online:
                state = "online"
            elif alias:
                state = "offline"
            else:
                state = "idle"

            out.append({
                "root": str(root),
                "name": child.name,
                "abs_path": str(child),
                "alias": alias,
                "online": online,
                "state": state,
                "daemon_pid": info.get("daemon_pid") if online else None,
            })

    def _sort_key(p):
        # 在线 → 离线 → 未运行
        band = {"online": 0, "offline": 1, "idle": 2}[p["state"]]
        return (band, p["root"].lower(), p["name"].lower())
    out.sort(key=_sort_key)
    return out
