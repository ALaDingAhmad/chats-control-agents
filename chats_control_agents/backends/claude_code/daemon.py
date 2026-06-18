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

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# Lifecycle: 通用 CLI 解析 / meta / pid 跟踪 / atexit 清理 — 见
# chats_control_agents/core/daemon_lifecycle.py 和 docs/BACKEND-DESIGN.md
from chats_control_agents.core import daemon_lifecycle as lc
from chats_control_agents.core.paths import ROOT

# Strip CSI escapes for ANSI-blind substring matching. Child claude is an
# Ink TUI: it renders text with cursor-move (\x1b[1C), SGR color
# (\x1b[38;2;R;G;Bm), and other CSI sequences interleaved between words.
# A naive `"trust this folder" in buffer` never matches a buffer that
# literally contains "\x1b[…mtrust\x1b[1Cthis\x1b[1Cfolder\x1b[…m" — both
# the color and cursor-right escapes split the substring. Strip every
# CSI sequence before scanning. Cursor-right also doesn't insert a space,
# so we additionally collapse adjacent letters (caller's responsibility:
# search for distinctive single words like "trust" if multi-word match
# is brittle).  See docs/DAEMON-LIFECYCLE.md.
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _ansi_blind(s: str) -> str:
    return _CSI.sub("", s)

try:
    from winpty import PtyProcess
except ImportError:
    print("ERROR: pywinpty not installed. Run: pip install pywinpty", file=sys.stderr)
    sys.exit(2)

def _find_claude_bin() -> Path:
    """Locate claude.exe: check PATH first, then npm default location.

    shutil.which may return a .cmd wrapper; winpty needs the real .exe,
    so we resolve through the wrapper's sibling node_modules tree.
    """
    import shutil
    found = shutil.which("claude")
    if found:
        p = Path(found).resolve()
        if p.suffix.lower() == ".cmd":
            real = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if real.exists():
                return real
        if p.suffix.lower() == ".exe":
            return p
    return (
        Path.home()
        / "AppData" / "Roaming" / "npm" / "node_modules"
        / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    )

CLAUDE_BIN = _find_claude_bin()
# Command to auto-type once the TUI is ready. We use the slash-command
# form (/chats-loop) because that's a deterministic skill route in Claude
# Code — the harness wires `/skill-name` directly to "invoke this skill",
# bypassing the LLM's fuzzy intent classifier that tends to treat
# imperative phrases like "启动 chats-loop" as "the user wants me to
# create a chats-loop application", which is what kept happening before.
TRIGGER_COMMAND = "/chats-loop"
# How many seconds of no new PTY output before we consider TUI settled
# and send the trigger command. Must be long enough that the TUI finishes
# rendering its welcome screen, but short enough that users don't wait
# unnecessarily. Trust-folder dialog resets this timer.
READY_SETTLE_SECS = 3

# claude_code 历史默认 spawn cwd：ccs 工具目录，让 child claude 用 CCS 当前
# 选中的账号（见 CLAUDE.md "daemon spawn child claude 的 cwd 不是 agent-bridge"）。
_HISTORICAL_CWD = ROOT.parent / "claude-code-account-switch"

# CLI: python -m chats_control_agents.backends.claude_code.daemon [<alias>] [<cwd>]
ALIAS, CWD_ARG = lc.parse_cli_args(default_cwd=_HISTORICAL_CWD)


def _decode(b: bytes | str) -> str:
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return repr(b)


def main() -> int:
    if not CLAUDE_BIN.exists():
        print(f"ERROR: claude.exe not found at {CLAUDE_BIN}", file=sys.stderr)
        return 2

    # 决定 spawn cwd（CLI > meta 历史 > ccs 兜底 > $HOME）
    spawn_cwd = lc.resolve_spawn_cwd(CWD_ARG, ALIAS, backend_default=_HISTORICAL_CWD)

    # 初始化生命周期：日志 / session_dir / 初始 meta（含 backend=claude_code）
    ctx = lc.init_lifecycle(alias=ALIAS, cwd=spawn_cwd, backend="claude_code")
    log = ctx.log
    log.info("claude=%s trigger='%s' settle=%ds", CLAUDE_BIN, TRIGGER_COMMAND, READY_SETTLE_SECS)
    print(f"[daemon] alias: {ALIAS}")
    print(f"[daemon] spawning {CLAUDE_BIN}")
    print(f"[daemon] session dir: {ctx.session_dir}")
    print(f"[daemon] cwd: {spawn_cwd}")

    # Open pty log fresh each run
    pty_log_path = ctx.session_dir / "pty.log"
    pty_log = open(pty_log_path, "w", encoding="utf-8", errors="replace")

    OUTBOX_PATH = ctx.session_dir / "outbox.txt"

    notice_seq = 0

    def _write_outbox_notice(text: str, *, icon: str = "⏳") -> None:
        nonlocal notice_seq
        notice_seq += 1
        stamp = datetime.now().strftime("%H:%M:%S")
        body = f"[{stamp}] {icon} {text}  (#{notice_seq})"
        try:
            OUTBOX_PATH.write_text(f"[{stamp}]\n{body}\n", encoding="utf-8")
        except Exception:
            pass

    _write_outbox_notice("正在启动 Claude…")

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

    # 补 meta：child_pid + 这个 backend 专属字段
    lc.write_meta(ctx, child_pid=proc.pid, trigger=TRIGGER_COMMAND)
    lc.record_spawned_child(ctx, proc.pid)

    # backend 专属清理：杀 child claude + 关 pty 日志
    def _on_exit() -> None:
        try:
            if proc.isalive():
                proc.terminate(force=True)
                log.info("cleanup: killed child pid=%s", proc.pid)
        except Exception as e:
            log.warning("cleanup kill failed: %s", e)
        try:
            pty_log.close()
        except Exception:
            pass

    lc.install_cleanup(ctx, on_exit=_on_exit)

    SESSION_DIR = ctx.session_dir

    # ── Startup loop: read PTY → handle trust dialog → wait for output to
    # settle → send trigger → wait for "loop active" → enter drain loop.
    #
    # No timeout exits. As long as claude.exe is alive and producing output,
    # we keep going and push status to outbox so the user sees what's happening.
    print("[daemon] reading PTY output...")
    buffer = ""
    trust_dismissed = False
    trigger_sent = False
    last_output_at = time.time()

    while proc.isalive():
        try:
            chunk = proc.read(1024)
        except Exception as e:
            log.warning("read failed: %s (alive=%s)", e, proc.isalive())
            if not proc.isalive():
                break
            time.sleep(0.1)
            continue

        if not chunk:
            # No output — check if settled long enough to send trigger
            if not trigger_sent and (time.time() - last_output_at >= READY_SETTLE_SECS):
                elapsed = time.time() - last_output_at
                log.info("TUI settled (%.1fs silence), sending trigger", elapsed)
                _write_outbox_notice("正在激活 chats-loop…")
                try:
                    proc.write(TRIGGER_COMMAND + "\r")
                    trigger_sent = True
                    log.info("sent trigger: %r", TRIGGER_COMMAND)
                except Exception as e:
                    log.warning("trigger write failed: %s", e)
                    _write_outbox_notice(f"trigger 发送失败: {e}", icon="❌")
                last_output_at = time.time()
            time.sleep(0.1)
            continue

        text = _decode(chunk)
        pty_log.write(text)
        pty_log.flush()
        buffer = (buffer + text)[-4096:]
        last_output_at = time.time()
        scan = _ansi_blind(buffer)

        # Auto-dismiss trust-folder dialog
        if not trust_dismissed and "trustthisfolder" in scan:
            try:
                proc.write("\r")
                trust_dismissed = True
                log.info("trust-folder dialog: pressed Enter (accept default)")
                _write_outbox_notice("已通过信任目录确认，等待加载…")
                buffer = ""
                last_output_at = time.time()
            except Exception as e:
                log.warning("trust-folder accept failed: %s", e)
            continue

        # Detect chats-loop activation — we're done with startup
        if trigger_sent and ("chats-loop loop active" in buffer or "loop active" in buffer):
            log.info("chats-loop activated")
            _write_outbox_notice("已就绪，发消息试试", icon="✅")
            break

    if not proc.isalive():
        log.error("claude.exe died during startup")
        _write_outbox_notice("Claude 进程在启动阶段退出", icon="❌")
        _cleanup()
        return 1

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

    RATE_LIMIT_MARKERS = ("You've hit your limit", "/rate-limit-options")
    PERMISSION_MARKERS = ("Allowonce", "Allowforsession", "Allowalways",
                          "allowthistool", "Allowtool")
    PERMISSION_COOLDOWN_SECS = 5
    RECOVERY_INTERVAL_SECS = 300
    RECOVERY_COOLDOWN_SECS = 60
    DETECT_WINDOW_BYTES = 4096

    pty_buffer = ""
    rate_limited = False
    last_press_3_at = 0.0
    last_trigger_retry_at = 0.0
    last_perm_accept_at = 0.0
    perm_accept_count = 0

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
                scan_blind = _ansi_blind(pty_buffer)
                # Detect permission dialog — auto-accept to prevent PTY freeze
                if any(m in scan_blind for m in PERMISSION_MARKERS):
                    now = time.time()
                    if now - last_perm_accept_at >= PERMISSION_COOLDOWN_SECS:
                        try:
                            proc.write("y")
                            last_perm_accept_at = now
                            perm_accept_count += 1
                            log.warning("permission dialog detected: sent 'y' (count=%d)", perm_accept_count)
                        except Exception as e:
                            log.warning("permission auto-accept failed: %s", e)
                        if perm_accept_count == 1:
                            _write_outbox_notice(
                                "检测到权限确认弹窗（--dangerously-skip-permissions 可能失效），"
                                "已自动批准。如反复出现请检查 Claude Code 版本。",
                                icon="⚠️",
                            )
                        pty_buffer = ""
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
                                "限额重置后自动恢复。如急用请到电脑切账号：ccs use <账号>",
                                icon="⚠️",
                            )
                        # Clear the buffer so we don't re-match the same text
                        pty_buffer = ""
                # Detect recovery: skill activation marker means we got back in
                if rate_limited and (
                    "chats-loop loop active" in pty_buffer
                    or "loop active" in pty_buffer
                ):
                    log.info("rate-limit recovery confirmed: loop active")
                    _write_outbox_notice("Claude 已恢复，可以继续聊了。", icon="✅")
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
