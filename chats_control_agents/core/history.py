"""Per-alias chat history (chat_sessions/<alias>/history.json) I/O.

History is a flat list of {role, text, ts, source} entries appended by
inbound message paths and by the backend's stop-hook. It's a human-readable
log; the agent does NOT see it. (Cross-restart memory is not provided.)
"""
from __future__ import annotations

import json
from datetime import datetime

from . import sessions as sx
from .paths import history_path


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_history(alias: str | None = None) -> list:
    if alias is None:
        alias = sx.get_current()
    if not alias:
        return []
    p = history_path(alias)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(items, alias: str | None = None) -> None:
    if alias is None:
        alias = sx.get_current()
    if not alias:
        return
    p = history_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
