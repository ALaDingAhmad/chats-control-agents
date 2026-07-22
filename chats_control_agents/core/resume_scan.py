"""Scan a project's past claude sessions for the resume menu.

Data source is ~/.claude/history.jsonl — the same file the native
`claude --resume` picker reads. Each line is one user turn:
{display, timestamp, project, sessionId}, where `display` is the raw text
the user typed (no slash-command template noise). We group by sessionId,
filter to the current project cwd, keep only sessions whose transcript still
exists (so --resume won't fail), and build a summary from each session's most
recent user inputs.

Why not scan the transcript's first user message (the old approach): sessions
that open with a fixed prompt (e.g. /recall → "继续") all produced identical
summaries, so the user couldn't tell them apart. history.jsonl's `display`
plus "last N inputs" distinguishes them by what was actually said.

Only claude_channel can actually resume (see docs/后端设计.md "resume 控制通路").
The contract for this menu lives in docs/入站路由.md "会话列表与摘要来源".
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from pathlib import Path

# How many recent sessions the resume menu shows.
RESUME_MENU_LIMIT = 5

# How many recent user inputs to stitch into each session's summary line.
SUMMARY_RECENT_INPUTS = 3

# Claude stores transcripts under ~/.claude/projects/<sanitized-cwd>/ and the
# cross-project input history in ~/.claude/history.jsonl.
_CLAUDE_DIR = Path.home() / ".claude"
_CLAUDE_PROJECTS_DIR = _CLAUDE_DIR / "projects"
_HISTORY_FILE = _CLAUDE_DIR / "history.jsonl"


def sanitized_project_dir(cwd: str) -> Path:
    """cwd → the ~/.claude/projects/<name> directory claude uses for it.

    Rule (matches Claude Code + the /recall command): replace every '/', ':',
    '\\' with '-', then strip a leading '-'. e.g.
    'D:\\aiproject\\foo' → 'D--aiproject-foo'.
    """
    name = cwd.replace("/", "-").replace(":", "-").replace("\\", "-")
    name = name.lstrip("-")
    return _CLAUDE_PROJECTS_DIR / name


def _existing_transcript_ids(cwd: str) -> set[str]:
    """Session ids that still have a transcript on disk (== resumable).

    A history entry whose transcript was cleaned up can't be --resume'd, so we
    filter those out of the menu (matches native `claude --resume`).
    """
    d = sanitized_project_dir(cwd)
    if not d.is_dir():
        return set()
    try:
        return {p.stem for p in d.glob("*.jsonl") if p.is_file()}
    except Exception:
        return set()


def _summary_from_inputs(displays: list[str], maxlen: int = 40) -> str:
    """Stitch a session's most recent user inputs into one phone-friendly line.

    `displays` is the session's inputs in chronological order. We take the last
    SUMMARY_RECENT_INPUTS, oldest→newest, join with ' · ', and trim to maxlen.
    """
    recent = [d.strip() for d in displays if d and d.strip()]
    recent = recent[-SUMMARY_RECENT_INPUTS:]
    if not recent:
        return "(无摘要)"
    line = " · ".join(recent)
    line = " ".join(line.split())  # collapse any embedded whitespace/newlines
    return line[:maxlen]


def list_recent_sessions(cwd: str, limit: int = RESUME_MENU_LIMIT) -> list[dict]:
    """Recent resumable claude sessions for a project cwd, newest first.

    Reads ~/.claude/history.jsonl, groups by sessionId filtered to `cwd`, keeps
    only sessions whose transcript still exists, sorts by each session's latest
    timestamp, and returns the top `limit`.

    Each item: {session_id, mtime, summary} — same shape the caller
    (_enter_resume_menu) expects. Empty list if there's no usable history
    (caller then falls back to a new blank session — see _cmd_pick_proj).
    """
    if not _HISTORY_FILE.is_file():
        return []

    # sessionId → {"inputs": [(ts, display), ...]} in file order.
    groups: "OrderedDict[str, list[tuple[float, str]]]" = OrderedDict()
    try:
        with _HISTORY_FILE.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("project") != cwd:
                    continue
                sid = d.get("sessionId")
                if not sid:
                    continue
                ts = d.get("timestamp") or 0
                disp = d.get("display") or ""
                groups.setdefault(sid, []).append((ts, disp))
    except Exception:
        return []

    if not groups:
        return []

    resumable = _existing_transcript_ids(cwd)
    if not resumable:
        return []  # no transcripts on disk → nothing is resumable

    rows: list[dict] = []
    for sid, items in groups.items():
        if sid not in resumable:
            continue  # transcript gone → can't --resume → hide (native parity)
        items.sort(key=lambda x: x[0])
        latest_ts = items[-1][0]
        displays = [disp for _, disp in items]
        rows.append({
            "session_id": sid,
            # history timestamps are epoch millis; mtime callers expect seconds.
            "mtime": (latest_ts / 1000.0) if latest_ts else 0.0,
            "summary": _summary_from_inputs(displays),
        })

    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows[:limit]
