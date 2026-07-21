"""Scan a project's claude transcript sessions for the resume menu.

Given a project cwd, find the recent Claude Code transcript files
(~/.claude/projects/<sanitized-cwd>/*.jsonl), newest first, and extract a
one-line human summary from each so the WeChat user can recognise which past
conversation to `--resume` into.

Only claude_channel can actually resume (see docs/后端设计.md "resume 控制通路"),
but the transcript directory holds *all* claude sessions run in that cwd —
including ones the user started by hand in a terminal. That is intentional:
it is what lets the phone "reconnect" to a conversation run on the desktop.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# How many recent sessions the resume menu shows.
RESUME_MENU_LIMIT = 5

# Claude stores transcripts under ~/.claude/projects/<sanitized-cwd>/.
_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# XML-ish wrappers that Claude Code injects around the first user turn
# (slash-command expansions, local-command output, system reminders, caveats).
# The raw first message is usually one of these, not something the human typed,
# so we skip them when building a summary.
_WRAPPER_TAG_RE = re.compile(
    r"<(command-message|command-name|command-args|local-command-[a-z]+|"
    r"system-reminder|caveat)[^>]*>.*?</\1>",
    re.DOTALL,
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def sanitized_project_dir(cwd: str) -> Path:
    """cwd → the ~/.claude/projects/<name> directory claude uses for it.

    Rule (matches Claude Code + the /recall command): replace every '/', ':',
    '\\' with '-', then strip a leading '-'. e.g.
    'D:\\aiproject\\foo' → 'D--aiproject-foo'.
    """
    name = cwd.replace("/", "-").replace(":", "-").replace("\\", "-")
    name = name.lstrip("-")
    return _CLAUDE_PROJECTS_DIR / name


def _extract_summary(jsonl_path: Path, maxlen: int = 40) -> str:
    """First human-readable line from a transcript, cleaned for a phone screen.

    Walks the transcript's user turns in order, skips Claude-Code wrapper
    boilerplate and tool results, and returns the first chunk of real text.
    Falls back to '(无摘要)' if nothing usable is found.
    """
    try:
        with jsonl_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                text = _content_to_text(content)
                if not text:
                    continue
                cleaned = _clean_text(text)
                if cleaned:
                    return cleaned[:maxlen]
    except Exception:
        pass
    return "(无摘要)"


def _content_to_text(content) -> str:
    """Flatten a message.content (str | list[block]) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                # skip tool_result / tool_use noise; keep text blocks
                if blk.get("type") == "text" and blk.get("text"):
                    parts.append(str(blk["text"]))
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(parts)
    return ""


def _clean_text(text: str) -> str:
    """Strip wrapper tags + collapse whitespace; '' if nothing human remains."""
    t = _WRAPPER_TAG_RE.sub("", text)
    t = _ANY_TAG_RE.sub("", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def list_recent_sessions(cwd: str, limit: int = RESUME_MENU_LIMIT) -> list[dict]:
    """Recent claude transcript sessions for a project cwd, newest first.

    Each item: {session_id, mtime, summary}. Empty list if the project has no
    transcript directory or no .jsonl files (caller then falls back to a new
    blank session — see _cmd_pick_proj).
    """
    d = sanitized_project_dir(cwd)
    if not d.is_dir():
        return []
    try:
        files = [p for p in d.glob("*.jsonl") if p.is_file()]
    except Exception:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for p in files[:limit]:
        out.append({
            "session_id": p.stem,          # filename without .jsonl == session id
            "mtime": p.stat().st_mtime,
            "summary": _extract_summary(p),
        })
    return out
