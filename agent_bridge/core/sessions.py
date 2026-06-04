"""Session registry: alias notion, current selection, liveness listing.

A *session* is a directory `chat_sessions/<alias>/` holding:
  inbox.txt          — last message a channel wrote for the backend
  outbox.txt         — last reply a backend wrote for the channel
  history.json       — full chronological message log (for UI display)
  meta.json          — {alias, cwd, daemon_pid, child_pid, created_at, …}
  spawned_pids.jsonl — append-only daemon-spawned child PIDs
  daemon.log, pty.log, daemon_stdout.log — daemon runtime logs

No command processing here — see commands.py.
"""
from __future__ import annotations

import json
from typing import Optional

from .paths import (
    ALIAS_RE,
    CURRENT_FILE,
    DEFAULT_ALIAS,
    SESSIONS_ROOT,
    history_path,
    inbox_path,
    meta_path,
    outbox_path,
    session_dir,
)
from .pid_track import _pid_alive


# ── Current selection (global, single-user) ──────────────────────────────
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


# ── Meta access ──────────────────────────────────────────────────────────
def load_meta_for(alias: str) -> Optional[dict]:
    p = meta_path(alias)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_meta_for(alias: str, meta: dict) -> None:
    p = meta_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


# ── Session listing ──────────────────────────────────────────────────────
def list_sessions() -> list[dict]:
    """Scan chat_sessions/ for all aliases. Sorted: online first, then by
    recency descending."""
    cur = get_current()
    out: list[dict] = []
    for entry in SESSIONS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        alias = entry.name
        if not ALIAS_RE.match(alias):
            continue
        m = load_meta_for(alias) or {}
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
    out.sort(key=lambda s: (not s["online"], -(s["last_active"] or 0)))
    return out


def _last_active(alias: str) -> Optional[float]:
    """Approximate last activity = newest of inbox/outbox/history mtime."""
    latest = 0.0
    for fn in (inbox_path, outbox_path, history_path):
        p = fn(alias)
        if p.exists():
            try:
                latest = max(latest, p.stat().st_mtime)
            except Exception:
                pass
    return latest or None


# ── Migration: pull legacy single-session files into chat_sessions/default/ ──
def migrate_legacy_if_present() -> None:
    """One-shot: if old chat_inbox.txt etc. exist at project root, move them
    into chat_sessions/default/. Safe to call on every startup."""
    from .paths import ROOT
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
            try:
                old.rename(ROOT / (old_name + ".legacy"))
            except Exception:
                pass
