"""Persistent state for the /proj numbered-pick flow.

After /proj outputs a numbered list, the user can reply with a bare integer
to pick. The state has to survive web_server restarts because the user's
listing may have been issued by a previous process lifetime — bug found
2026-06-03 when picking "20" silently went to chat instead of switching
projects.
"""
from __future__ import annotations

import json
import time

from .paths import PROJ_CHOICES_FILE


# After /proj, a bare integer within this many seconds picks a project.
PROJ_PICK_WINDOW_SECS = 120


def read_proj_choices() -> dict | None:
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


def write_proj_choices(choices: dict | None) -> None:
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


def proj_choices_active() -> bool:
    return read_proj_choices() is not None
