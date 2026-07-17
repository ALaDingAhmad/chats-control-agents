"""
Web-chat MCP bridge (long-poll edition, multi-session aware).

Each Claude Code daemon serves one session identified by an `alias`.
The alias is read from env var CHATS_LOOP_ALIAS at startup; falls back to
"default" so single-window usage keeps working.

Per-session IO lives at:
  chat_sessions/<alias>/inbox.txt
  chat_sessions/<alias>/outbox.txt

Tools:
  - wait_for_message(): block until a message arrives, then return it.
    Internally uses exponential backoff (300s → 600s → ...) for empty polls.
  - send_chat_response(reply): write Claude's reply for the bridge to pick up.

Log: ./mcp_bridge.log  (shared across sessions, prefixed by alias)
"""
import atexit
import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime

from mcp.server.fastmcp import FastMCP

# Project root = grandparent of chats_control_agents package = 3 parents up from this file
# (chats_control_agents/backends/claude_code/mcp_bridge.py → parents[3])
ROOT = Path(__file__).resolve().parents[3]
LOG_PATH = ROOT / "mcp_bridge.log"

# Alias resolution order:
#   1. CHATS_LOOP_ALIAS env (set by daemon when it spawns child claude)
#   2. <basename(cwd)>-<MMDD-HHMM> (auto for user-opened claude windows so
#      every window gets a unique alias, no more "default" collision)
#   3. relay_init() MCP tool can override at runtime (used by the chats-loop
#      skill when the user wants an explicit alias).
def _initial_alias() -> str:
    env_val = (os.environ.get("CHATS_LOOP_ALIAS") or "").strip()
    if env_val:
        return env_val
    # Defer import so this file can be invoked stand-alone via python path
    sys.path.insert(0, str(ROOT))
    from chats_control_agents.core.sessions import make_alias_for_cwd  # noqa: E402
    return make_alias_for_cwd(os.getcwd())


def _ensure_meta(alias: str, session_dir: Path) -> None:
    """Ensure meta.json exists and records our bridge_pid so web/dashboard can discover this session.

    Registration only — NEVER touches _current.txt. Becoming the current
    session requires an explicit user pick (/proj, /use, dashboard); a bridge
    that grabs routing on startup hijacks all inbound the moment any claude
    window opens (docs/ROUTING.md "终端 chats-loop 会话").
    """
    meta_file = session_dir / "meta.json"
    bridge_info: dict = {"bridge_pid": os.getpid()}
    try:
        import psutil
        bridge_info["bridge_create_time"] = psutil.Process(os.getpid()).create_time()
    except Exception:
        pass

    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        meta.update(bridge_info)
    else:
        meta = {
            "alias": alias,
            "cwd": os.getcwd(),
            "backend": "claude_code",
            "daemon_pid": None,
            "child_pid": None,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            **bridge_info,
        }
    try:
        tmp = meta_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(meta_file)
    except Exception:
        pass


ALIAS = _initial_alias()
SESSION_DIR = ROOT / "chat_sessions" / ALIAS
SESSION_DIR.mkdir(parents=True, exist_ok=True)
_ensure_meta(ALIAS, SESSION_DIR)
INBOX = SESSION_DIR / "inbox.txt"
OUTBOX = SESSION_DIR / "outbox.txt"
# Marker file watched by ~/.claude/hooks/chats_loop_pretool_hook.py.
# Created on first wait_for_message call (relay loop is active).
# Removed at process exit. Per-alias so multiple daemons don't fight.
MARKER = Path.home() / ".claude" / f".chats-loop-active-{ALIAS}"

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("bridge")


def _retarget_alias(new_alias: str) -> None:
    """relay_init swaps the active session at runtime. Updates all module-level
    paths and rotates the marker file. Safe to call multiple times."""
    global ALIAS, SESSION_DIR, INBOX, OUTBOX, MARKER
    old_marker = MARKER
    ALIAS = new_alias
    SESSION_DIR = ROOT / "chat_sessions" / ALIAS
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_meta(ALIAS, SESSION_DIR)
    INBOX = SESSION_DIR / "inbox.txt"
    OUTBOX = SESSION_DIR / "outbox.txt"
    MARKER = Path.home() / ".claude" / f".chats-loop-active-{ALIAS}"
    try:
        if old_marker.exists() and old_marker != MARKER:
            old_marker.unlink()
    except Exception as e:
        log.warning("[%s] retarget: old marker unlink failed: %s", new_alias, e)
    log.info("[%s] alias retargeted from previous", new_alias)



mcp = FastMCP("cca-msg")

# Exponential backoff for empty polls. mcp_bridge OWNS this strategy — the
# `timeout_seconds` argument from Claude is ignored. Rationale:
#   - User idle → save tokens by polling less often.
#   - User active → first wait is short enough that response feels instant.
# Sequence: 300s (5min) → 600s → 1200s → 2400s → ... doubling forever.
# A real inbound message resets the counter back to 0.
_consecutive_timeouts = 0
BASE_WAIT_SECONDS = 300  # first timeout, doubles after each empty wait


def _current_wait_seconds() -> int:
    return BASE_WAIT_SECONDS * (2 ** _consecutive_timeouts)


@mcp.tool()
def relay_init(alias: str) -> str:
    """
    Switch this MCP server's active alias. Call this before wait_for_message
    when you want messages routed to a session other than the auto-derived
    one (the chats-loop skill uses this to set a `<project>-<MMDD-HHMM>` alias).

    Alias must match a-zA-Z0-9_- or CJK characters, 1-32 chars.
    """
    import re as _re
    if not _re.match(r"^[a-zA-Z0-9_\-一-鿿]{1,32}$", alias):
        return f"ERROR: invalid alias {alias!r}. Must be a-zA-Z0-9_- or CJK, 1-32 chars."
    _retarget_alias(alias)
    return f"OK, alias is now {alias}. Inbox: {INBOX}"


@mcp.tool()
def wait_for_message(timeout_seconds: int = 0) -> str:
    """
    Block until the web user sends a message, then return its text.

    The wait duration is managed internally by exponential backoff and the
    `timeout_seconds` argument is IGNORED. The first wait is 5 minutes; each
    subsequent timeout doubles the wait. Receiving a real message resets the
    backoff to the base.

    Returns a real message string when one arrives, or a string starting with
    "TIMEOUT" when the wait elapses with no message. The TIMEOUT string also
    reports how long the next wait will be, e.g.:
        "TIMEOUT (waited 300s, next will be 600s)"

    Just call this again after any TIMEOUT — the loop never ends until the
    user explicitly stops it.
    """
    global _consecutive_timeouts
    wait_secs = _current_wait_seconds()
    # Mark relay-active so the global Stop hook knows to mirror terminal text
    # back to the web UI for this session.
    try:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.touch(exist_ok=True)
    except Exception as e:
        log.warning("marker touch failed: %s", e)
    log.info(
        "wait_for_message: consec_timeouts=%d → waiting up to %ds",
        _consecutive_timeouts, wait_secs,
    )
    deadline = time.time() + wait_secs
    poll_interval = 0.5
    last_touch = time.time()

    while time.time() < deadline:
        # Heartbeat lease: keep marker mtime fresh only while actually
        # blocked in this wait — liveness checks read freshness, not
        # existence, to tell "waiting" from "loop stopped / stale leftover".
        if time.time() - last_touch >= 5.0:
            try:
                MARKER.touch(exist_ok=True)
            except Exception:
                pass
            last_touch = time.time()
        if INBOX.exists():
            try:
                text = INBOX.read_text(encoding="utf-8").strip()
            except Exception as e:
                log.warning("inbox read failed: %s", e)
                text = ""
            if text:
                log.info("  got msg %d chars: %r", len(text), text[:200])
                INBOX.write_text("", encoding="utf-8")
                _consecutive_timeouts = 0  # active user → reset backoff
                return text
        time.sleep(poll_interval)

    _consecutive_timeouts += 1
    next_wait = _current_wait_seconds()
    log.info(
        "  timeout, no msg in %ds (consec=%d, next wait=%ds)",
        wait_secs, _consecutive_timeouts, next_wait,
    )
    return f"TIMEOUT (waited {wait_secs}s, next will be {next_wait}s)"


@mcp.tool()
def send_chat_response(reply: str) -> str:
    """
    Send a reply back to the web user. Call this after composing your answer.

    After this returns, immediately call wait_for_message again to keep the
    session alive — the loop never ends until the user explicitly stops it.
    """
    log.info("send_chat_response called, %d chars", len(reply))
    try:
        MARKER.touch(exist_ok=True)  # replying = still serving; renew lease
    except Exception:
        pass
    stamp = datetime.now().strftime("%H:%M:%S")
    OUTBOX.write_text(f"[{stamp}]\n{reply}\n", encoding="utf-8")
    return f"OK, sent {len(reply)} chars. Now call wait_for_message to await next user message."


def _cleanup_marker():
    try:
        if MARKER.exists():
            MARKER.unlink()
            log.info("marker removed at exit")
    except Exception as e:
        log.warning("marker cleanup failed: %s", e)


atexit.register(_cleanup_marker)


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("cca-msg bridge starting, inbox=%s outbox=%s", INBOX, OUTBOX)
    mcp.run()
