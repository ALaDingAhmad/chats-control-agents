"""Persistent state for the second-level resume session-pick flow.

After the user picks a project via /proj, we don't start a session directly.
Instead we list that project's recent claude transcript sessions (the .jsonl
files under ~/.claude/projects/<cwd>/) and arm this token; a bare integer then
picks a session to `--resume` into. Mirrors proj_choices.py — same one-shot
semantics, same web_server-restart survival rationale.

See docs/入站路由.md "两级菜单" and docs/后端设计.md "resume 控制通路".
"""
from __future__ import annotations

import json
import time

from .paths import RESUME_CHOICES_FILE


# After a project pick, a bare integer within this many seconds picks a session.
RESUME_PICK_WINDOW_SECS = 120


def read_resume_choices() -> dict | None:
    if not RESUME_CHOICES_FILE.exists():
        return None
    try:
        data = json.loads(RESUME_CHOICES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if data.get("expires_at", 0) < time.time():
            try:
                RESUME_CHOICES_FILE.unlink()
            except Exception:
                pass
            return None
        return data
    except Exception:
        return None


def write_resume_choices(choices: dict | None) -> None:
    if choices is None:
        try:
            RESUME_CHOICES_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return
    try:
        tmp = RESUME_CHOICES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(choices, ensure_ascii=False), encoding="utf-8")
        tmp.replace(RESUME_CHOICES_FILE)
    except Exception:
        pass


def resume_choices_active() -> bool:
    return read_resume_choices() is not None
