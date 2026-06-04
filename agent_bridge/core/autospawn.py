"""Autospawn queue.

When the user picks a project that isn't yet running (no daemon), the command
layer writes a request here. web_server's autospawn worker drains the file
and spawns `claude_daemon.py <alias> <cwd>` detached.

Decouples sessions/commands (pure logic) from process spawning (in web_server).
"""
from __future__ import annotations

import json
from datetime import datetime

from .paths import AUTOSPAWN_QUEUE_FILE


def request_autospawn(alias: str, cwd: str) -> None:
    rec = {
        "alias": alias,
        "cwd": cwd,
        "requested_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with AUTOSPAWN_QUEUE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
