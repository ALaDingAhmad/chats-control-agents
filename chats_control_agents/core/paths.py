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
import time
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

# 默认 backend：命令行 /proj 建会话时读这里决定起哪个 daemon。
# 缺省 claude_code；用户通过 /backend <name> 切换并写进此文件。
DEFAULT_BACKEND_FILE = SESSIONS_ROOT / "_default_backend.txt"

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


def control_path(alias: str) -> Path:
    return session_dir(alias) / "pty_control.txt"


def control_mode_path(alias: str) -> Path:
    return session_dir(alias) / "pty_control_mode.txt"


def loop_marker_path(alias: str) -> Path:
    """chats-loop 激活 marker：mcp_bridge 在 wait_for_message 期间心跳 touch，
    进程退出时删。"出现" = skill 已激活（watch_ready 用）；"当前在收件"
    要看 mtime 新鲜度，用 loop_marker_fresh()（docs/入站路由.md "就绪通知/信号源"）。"""
    return Path.home() / ".claude" / f".chats-loop-active-{alias}"


# marker 心跳租约 TTL。mcp_bridge 在 wait 阻塞期间每 ~5s touch 一次，
# 循环停了 mtime 不再刷新，超过 TTL 即判"不在收件"——哪怕文件还在
# （硬杀进程会留残留）、哪怕 bridge 进程还活着（只是挂着 MCP 没跑循环）。
LOOP_MARKER_TTL_SECS = 180.0


def loop_marker_fresh(alias: str, ttl: float = LOOP_MARKER_TTL_SECS) -> bool:
    """True = chats-loop 循环当前真的在收件（marker mtime 在 TTL 内）。
    在线/可服务判定一律用本函数，不许只查 bridge PID 或 marker 存在性。"""
    try:
        return (time.time() - loop_marker_path(alias).stat().st_mtime) <= ttl
    except OSError:
        return False
