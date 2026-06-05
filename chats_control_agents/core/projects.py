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
from pathlib import Path

from .config import get_workspace_roots
from .paths import ALIAS_RE, SESSIONS_ROOT
from .pid_track import _pid_alive


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

    def _sort_key(p):
        band = 0 if p["online"] else (1 if p["alias"] else 2)
        return (band, p["root"].lower(), p["name"].lower())
    out.sort(key=_sort_key)
    return out
