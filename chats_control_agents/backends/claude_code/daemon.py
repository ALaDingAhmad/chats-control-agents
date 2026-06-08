"""
Claude Code TUI daemon — Phase 1 POC.

Spawns claude.exe inside a winpty virtual terminal, waits for the TUI to
finish loading, then auto-types "启动 chats-loop\\r" so the chats-loop skill
takes over and the process sits in the wait_for_message loop.

What this proves (or disproves):
  - Can Claude Code run with no real terminal attached?
  - Can we deliver keystrokes programmatically?
  - Does the chats-loop skill actually trigger when fed via PTY?

Usage:
  python D:/aiproject/claude-mcp-bridge/claude_daemon.py

Log:    ./claude_daemon.log  — daemon's own events
Output: ./claude_pty.log     — raw PTY output from claude.exe (for debugging)

Stop:   Ctrl+C in the daemon's terminal, OR `taskkill /pid <pid>` on the
        daemon process. The daemon kills its child claude.exe before exit.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from winpty import PtyProcess
except ImportError:
    print("ERROR: pywinpty not installed. Run: pip install pywinpty", file=sys.stderr)
    sys.exit(2)

# Project root: chats_control_agents/backends/claude_code/daemon.py → parents[3]
ROOT = Path(__file__).resolve().parents[3]
# Per-alias log files so multiple daemons don't trample each other
ALIAS_RE = re.compile(r"^[a-zA-Z0-9_\-一-鿿]{1,32}$")
CLAUDE_BIN = (
    Path.home()
    / "AppData"
    / "Roaming"
    / "npm"
    / "node_modules"
    / "@anthropic-ai"
    / "claude-code"
    / "bin"
    / "claude.exe"
)
# Command to auto-type once the TUI is ready. We use the slash-command
# form (/chats-loop) because that's a deterministic skill route in Claude
# Code — the harness wires `/skill-name` directly to "invoke this skill",
# bypassing the LLM's fuzzy intent classifier that tends to treat
# imperative phrases like "启动 chats-loop" as "the user wants me to
# create a chats-loop application", which is what kept happening before.
TRIGGER_COMMAND = "/chats-loop"
# Heuristics for "TUI is ready" — any of these substrings appearing in PTY
# output after spawn means we can safely send the trigger.
READY_MARKERS = [
    "Welcome",          # Welcome back, Welcome to Claude Code
    "Tips for getting", # Tips for getting started
    "What's new",       # changelog block
    "│",                # box-drawing characters used heavily by ink TUI
    ">",                # prompt
    "❯",                # alternate prompt char some shells use
]
# Max seconds to wait for TUI ready before bailing
READY_TIMEOUT = 30
# How long after sending trigger before we consider it "successfully entered"
POST_TRIGGER_SETTLE = 6

def _parse_args() -> tuple[str, str | None]:
    """CLI: python -m chats_control_agents.backends.claude_code.daemon [<alias>] [<cwd>]

    Both optional. If alias is omitted, one is generated from cwd as
    `<basename>-<MMDD-HHMM>`. If both are omitted, cwd falls back to
    claude-code-account-switch (the historical default spawn dir).
    """
    args = sys.argv[1:]
    alias: str | None = None
    cwd: str | None = None
    # If first arg looks like an existing dir, treat all positional as cwd-only
    # (legacy: previously CLI accepted just a cwd path).
    if args and Path(args[0]).is_dir() and ("/" in args[0] or "\\" in args[0]):
        cwd = args[0]
    elif args:
        if not ALIAS_RE.match(args[0]):
            print(f"ERROR: invalid alias '{args[0]}'. allowed: a-zA-Z0-9_- and Chinese, 1-32 chars", file=sys.stderr)
            sys.exit(2)
        alias = args[0]
        if len(args) >= 2 and Path(args[1]).is_dir():
            cwd = args[1]
    if alias is None:
        # Defer import: chats_control_agents isn't on sys.path until daemon.py runs as -m
        from chats_control_agents.core.sessions import make_alias_for_cwd
        from chats_control_agents.core.paths import ROOT as _ROOT
        # cwd for naming purposes: caller-given, else historical default
        naming_cwd = cwd or str(_ROOT.parent / "claude-code-account-switch")
        alias = make_alias_for_cwd(naming_cwd)
    return alias, cwd


ALIAS, CWD_ARG = _parse_args()
SESSION_DIR = ROOT / "chat_sessions" / ALIAS
SESSION_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = SESSION_DIR / "daemon.log"
PTY_LOG_PATH = SESSION_DIR / "pty.log"
META_PATH = SESSION_DIR / "meta.json"

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("daemon")


def _decode(b: bytes | str) -> str:
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return repr(b)


def _load_meta() -> dict | None:
    if not META_PATH.exists():
        return None
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_meta(meta: dict) -> None:
    tmp = META_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(META_PATH)


def main() -> int:
    if not CLAUDE_BIN.exists():
        log.error("claude.exe not found at %s", CLAUDE_BIN)
        print(f"ERROR: claude.exe not found at {CLAUDE_BIN}", file=sys.stderr)
        return 2

    log.info("=" * 60)
    log.info("daemon starting alias=%s claude=%s", ALIAS, CLAUDE_BIN)
    log.info("trigger='%s', ready_timeout=%ds", TRIGGER_COMMAND, READY_TIMEOUT)
    print(f"[daemon] alias: {ALIAS}")
    print(f"[daemon] spawning {CLAUDE_BIN}")
    print(f"[daemon] session dir: {SESSION_DIR}")

    # Open pty log fresh each run
    pty_log = open(PTY_LOG_PATH, "w", encoding="utf-8", errors="replace")

    # Cwd: CLI arg → meta.json saved cwd → claude-code-account-switch → home
    if CWD_ARG and Path(CWD_ARG).is_dir():
        spawn_cwd = CWD_ARG
    else:
        prev_meta = _load_meta()
        prev_cwd = (prev_meta or {}).get("cwd") if prev_meta else None
        if prev_cwd and Path(prev_cwd).is_dir():
            spawn_cwd = prev_cwd
        else:
            spawn_cwd = str(Path(__file__).parent.parent / "claude-code-account-switch")
    if not Path(spawn_cwd).exists():
        spawn_cwd = str(Path.home())  # safe fallback
    log.info("spawn cwd=%s", spawn_cwd)
    print(f"[daemon] cwd: {spawn_cwd}")

    # Spawn with --dangerously-skip-permissions so mcp tool calls don't pop
    # interactive prompts. Crucially: set CHATS_LOOP_ALIAS so the child claude
    # → mcp_bridge subprocess knows which session it serves.
    spawn_env = {**os.environ, "CHATS_LOOP_ALIAS": ALIAS}
    proc = PtyProcess.spawn(
        [str(CLAUDE_BIN), "--dangerously-skip-permissions"],
        dimensions=(40, 200),
        cwd=spawn_cwd,
        env=spawn_env,
    )
    log.info("spawned pid=%s", proc.pid)
    print(f"[daemon] claude pid={proc.pid}")

    # Write meta so web_server can list sessions and check liveness
    _write_meta({
        "alias": ALIAS,
        "cwd": spawn_cwd,
        "daemon_pid": os.getpid(),
        "child_pid": proc.pid,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "trigger": TRIGGER_COMMAND,
    })

    # Append child PID + create_time to spawned_pids.jsonl so cleanup tooling
    # can later identify daemon-spawned children even after this daemon exits.
    # See sessions.list_daemon_child_pids() for the read side.
    try:
        import psutil
        ct = psutil.Process(proc.pid).create_time()
    except Exception as e:
        log.warning("psutil create_time failed for child %s: %s", proc.pid, e)
        ct = None
    try:
        rec = {
            "pid": proc.pid,
            "create_time": ct,
            "spawned_at": datetime.now().isoformat(timespec="seconds"),
            "daemon_pid": os.getpid(),
        }
        with (SESSION_DIR / "spawned_pids.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        log.warning("append spawned_pids.jsonl failed: %s", e)

    # Make sure we kill the child if daemon dies unexpectedly
    def _cleanup():
        try:
            if proc.isalive():
                proc.terminate(force=True)
                log.info("cleanup: killed child pid=%s", proc.pid)
        except Exception as e:
            log.warning("cleanup failed: %s", e)
        try:
            pty_log.close()
        except Exception:
            pass
        # Mark meta as offline (keep file so /list still shows the session)
        try:
            m = _load_meta() or {}
            m["daemon_pid"] = None
            m["child_pid"] = None
            m["last_exit_at"] = datetime.now().isoformat(timespec="seconds")
            _write_meta(m)
        except Exception:
            pass

    atexit.register(_cleanup)

    def _sigint(signum, frame):
        log.info("SIGINT received, shutting down")
        print("\n[daemon] shutting down...")
        _cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint)

    # Phase 1: wait for TUI to be "ready"
    print("[daemon] waiting for TUI to load...")
    buffer = ""
    start = time.time()
    ready = False
    while time.time() - start < READY_TIMEOUT:
        try:
            chunk = proc.read(1024)
        except Exception as e:
            # winpty raises on closed pty
            log.warning("read failed during ready wait: %s (alive=%s)", e, proc.isalive())
            if not proc.isalive():
                break
            time.sleep(0.1)
            continue
        if not chunk:
            time.sleep(0.05)
            continue
        text = _decode(chunk)
        pty_log.write(text)
        pty_log.flush()
        buffer += text
        # Look for any ready marker
        if any(marker in buffer for marker in READY_MARKERS):
            ready = True
            log.info("TUI ready after %.1fs (saw marker)", time.time() - start)
            print(f"[daemon] TUI ready after {time.time() - start:.1f}s")
            break

    if not ready:
        log.error("TUI never showed ready marker within %ds; last 500 bytes: %r",
                  READY_TIMEOUT, buffer[-500:])
        print(f"[daemon] FAIL: TUI did not become ready within {READY_TIMEOUT}s")
        print(f"[daemon] check {PTY_LOG_PATH} for what was emitted")
        _cleanup()
        return 1

    # Tiny extra settle so the input box is definitely focused
    time.sleep(1.5)

    # Phase 2: send the trigger command
    log.info("sending trigger: %r", TRIGGER_COMMAND)
    print(f"[daemon] sending: {TRIGGER_COMMAND}")
    try:
        # \r is the enter key on Windows TUIs
        proc.write(TRIGGER_COMMAND + "\r")
    except Exception as e:
        log.exception("write failed: %s", e)
        print(f"[daemon] FAIL: could not write to PTY: {e}")
        _cleanup()
        return 1

    # Phase 3: confirm the trigger was accepted by reading next few seconds of
    # output. We're looking for the skill's "chats-loop loop active" line.
    print("[daemon] waiting for skill activation confirmation...")
    confirm_buffer = ""
    confirm_start = time.time()
    activated = False
    while time.time() - confirm_start < POST_TRIGGER_SETTLE * 3:
        try:
            chunk = proc.read(1024)
        except Exception:
            if not proc.isalive():
                break
            time.sleep(0.1)
            continue
        if not chunk:
            time.sleep(0.05)
            continue
        text = _decode(chunk)
        pty_log.write(text)
        pty_log.flush()
        confirm_buffer += text
        if "chats-loop loop active" in confirm_buffer or "loop active" in confirm_buffer:
            activated = True
            log.info("skill activated")
            print("[daemon] OK: chats-loop loop active")
            break

    if not activated:
        log.warning("did not see 'chats-loop loop active' within %ds; continuing anyway "
                    "(claude may have started loop without printing the marker)",
                    POST_TRIGGER_SETTLE * 3)
        print("[daemon] WARN: did not see activation marker, but claude is alive — "
              "check chat_outbox.txt when you send a test message")

    # Phase 4: drain loop with rate-limit watchdog.
    #
    # When Claude's API limit is hit, the TUI shows a "You've hit your limit"
    # message and the /rate-limit-options dialog (1. Upgrade / 2. Team /
    # 3. Stop and wait). Without a real keyboard the dialog is never
    # dismissed, so the child claude freezes indefinitely.
    #
    # Watchdog behaviour:
    #   1. Detect the limit prompt in PTY output → auto-press "3\r" to choose
    #      "Stop and wait", which closes the dialog and lets the TUI go idle.
    #   2. Write a user-facing notice straight to outbox.txt so the bridge
    #      forwards it to the WeChat user / browser. Bypass child claude
    #      entirely — it can't respond while rate-limited.
    #   3. Every ~5 minutes, re-send the trigger command. If the limit has
    #      reset, the next call goes through and the message loop resumes.
    #      If not, the prompt re-appears and step 1 catches it again.
    print("[daemon] running. Ctrl+C to stop. Daemon now drains PTY output to claude_pty.log")
    log.info("entering drain loop")

    OUTBOX_PATH = SESSION_DIR / "outbox.txt"
    RATE_LIMIT_MARKERS = ("You've hit your limit", "/rate-limit-options")
    RECOVERY_INTERVAL_SECS = 300  # 5 min — re-send trigger while rate-limited
    RECOVERY_COOLDOWN_SECS = 60   # min gap between two "press 3" attempts
    DETECT_WINDOW_BYTES = 4096    # rolling buffer scanned for the marker

    pty_buffer = ""               # rolling tail of recent decoded PTY output
    rate_limited = False
    last_press_3_at = 0.0
    last_trigger_retry_at = 0.0
    notice_seq = 0                # ensures successive outbox writes look new

    def _write_outbox_notice(text: str) -> None:
        nonlocal notice_seq
        notice_seq += 1
        stamp = datetime.now().strftime("%H:%M:%S")
        # Use a sequence number so identical retries don't get dedup'd by
        # web_server's outbox watcher fingerprint.
        body = f"[{stamp}] ⚠️ {text}  (#{notice_seq})"
        try:
            OUTBOX_PATH.write_text(f"[{stamp}]\n{body}\n", encoding="utf-8")
            log.info("outbox notice written: %s", text[:80])
        except Exception as e:
            log.warning("outbox notice write failed: %s", e)

    def _press_3() -> None:
        nonlocal last_press_3_at
        try:
            proc.write("3\r")
            last_press_3_at = time.time()
            log.info("rate-limit dialog detected: sent '3\\r'")
        except Exception as e:
            log.warning("press 3 failed: %s", e)

    def _retry_trigger() -> None:
        nonlocal last_trigger_retry_at
        try:
            proc.write(TRIGGER_COMMAND + "\r")
            last_trigger_retry_at = time.time()
            log.info("rate-limit recovery: re-sent trigger")
        except Exception as e:
            log.warning("retry trigger failed: %s", e)

    while proc.isalive():
        try:
            chunk = proc.read(4096)
            if chunk:
                text = _decode(chunk)
                pty_log.write(text)
                pty_log.flush()
                pty_buffer = (pty_buffer + text)[-DETECT_WINDOW_BYTES:]
                # Detect rate-limit dialog
                if any(m in pty_buffer for m in RATE_LIMIT_MARKERS):
                    now = time.time()
                    if now - last_press_3_at >= RECOVERY_COOLDOWN_SECS:
                        _press_3()
                        if not rate_limited:
                            rate_limited = True
                            # Anchor retry cooldown to now — otherwise the
                            # 0.0 initial value triggers an immediate retry
                            # that will be rejected for sure.
                            last_trigger_retry_at = now
                            _write_outbox_notice(
                                "Claude 账号已撞用量上限。Bridge 会每 5 分钟重试，"
                                "限额重置后自动恢复。如急用请到电脑切账号："
                                "cd D:/aiproject/claude-code-account-switch && ccs use <账号>"
                            )
                        # Clear the buffer so we don't re-match the same text
                        pty_buffer = ""
                # Detect recovery: skill activation marker means we got back in
                if rate_limited and (
                    "chats-loop loop active" in pty_buffer
                    or "loop active" in pty_buffer
                ):
                    log.info("rate-limit recovery confirmed: loop active")
                    _write_outbox_notice("Claude 已恢复，可以继续聊了。")
                    rate_limited = False
                    pty_buffer = ""
            else:
                time.sleep(0.2)

            # Periodic retry while rate-limited
            if rate_limited:
                now = time.time()
                if now - last_trigger_retry_at >= RECOVERY_INTERVAL_SECS:
                    _retry_trigger()

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.warning("drain read failed: %s", e)
            time.sleep(0.5)

    log.info("claude process exited")
    print("[daemon] claude exited")
    _cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
