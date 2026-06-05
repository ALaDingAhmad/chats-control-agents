"""Filesystem paths and the alias regex.

These are the lowest-level constants of the package — everything that touches
disk imports from here. Keep it dependency-free.

Paths are anchored at the *project root* (`chats_control_agents`'s grandparent),
NOT at this file. This is so the running daemon, which spawns from the
project root, ends up with the same chat_sessions/ directory regardless of
how Python found the package.
"""
from __future__ import annotations

import re
from pathlib import Path

# Project root = directory containing the `chats_control_agents` package
# (this file is at chats_control_agents/core/paths.py → parents[2])
ROOT = Path(__file__).resolve().parents[2]

# Per-session IO + state
SESSIONS_ROOT = ROOT / "chat_sessions"
SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)
CURRENT_FILE = SESSIONS_ROOT / "_current.txt"

# Workspace + bridge config
CONFIG_FILE = ROOT / "config.json"

# /proj numeric-pick state (persisted across web_server restarts)
PROJ_CHOICES_FILE = SESSIONS_ROOT / "_pending_proj.json"

# Queue: web_server's autospawn worker drains this
AUTOSPAWN_QUEUE_FILE = SESSIONS_ROOT / "_autospawn_queue.jsonl"

# Alias = a-zA-Z0-9_-CJK 1-32 chars. Used as a directory name and command arg.
ALIAS_RE = re.compile(r"^[a-zA-Z0-9_\-一-鿿]{1,32}$")

# Legacy: pre-multi-session code wrote everything under chat_sessions/default/.
# No new code creates this alias; kept only so the one-shot legacy migration
# function still has a target and so existing default/ dirs remain readable.
LEGACY_DEFAULT_ALIAS = "default"


# ── Per-alias path helpers ────────────────────────────────────────────────
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


def spawned_log_path(alias: str) -> Path:
    return session_dir(alias) / "spawned_pids.jsonl"
