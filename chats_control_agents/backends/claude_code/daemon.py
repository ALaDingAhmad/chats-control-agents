from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from chats_control_agents.core import daemon_lifecycle as lc
from chats_control_agents.core.paths import ROOT, control_mode_path, control_path

try:
    from winpty import PtyProcess
except ImportError:
    print("ERROR: pywinpty not installed. Run: pip install pywinpty", file=sys.stderr)
    sys.exit(2)


_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")
_TUI_LOADED_MARKERS = ("effort", "bypass", "Try")
_AUTH_ERROR_MARKERS = (
    "Please run /login",
    "Invalid authentication credentials",
    "authentication_error",
)
_MEANINGFUL_LINE_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]{3,}")
_NOISE_PATTERNS = (
    "bypasspermissionson",
    "shift+tabtocycle",
    "workedfor",
    "cogitatedfor",
    "contextleft",
    "opus4.6",
    "sonnet",
    "haiku",
    "cursordisconnected",
    "@desktop-",
    "<motardmohamed40@gmail.com>",
    "[pro]",
    "ctx:",
    "$0.",
    "5h:",
    "7d:",
    "msys",
    "(ccs",
    "what'snew",
    "releasenotes",
    "release-notes",
    "youhavelaunchedclaudeinyourhomedirectory",
    "forthebestexperience",
    "organization",
    "try\"createautil",
)
_NOISE_REGEXES = (
    re.compile(r"ctx:\d+%.*\$0\.\d+.*5h:\d+%.*7d:\d+%", re.IGNORECASE),
    re.compile(r"\$0\.\d+.*5h:\d+%.*7d:\d+%", re.IGNORECASE),
    re.compile(r"^[a-z0-9_-]+@desktop-", re.IGNORECASE),
)

TRIGGER_COMMAND = "/chats-loop"
READY_SETTLE_SECS = 3
AUTO_WAKE_INTERVAL_SECS = 8.0
_HISTORICAL_CWD = ROOT.parent / "claude-code-account-switch"
ALIAS, CWD_ARG = lc.parse_cli_args(default_cwd=_HISTORICAL_CWD)


def _ansi_blind(s: str) -> str:
    return _CSI.sub("", _OSC.sub("", s))


def _pty_plain_text(s: str) -> str:
    s = _ansi_blind(s).replace("\r", "\n")
    cleaned = []
    for ch in s:
        if ch in ("\n", "\t") or ord(ch) >= 32:
            cleaned.append(ch)
    text = "".join(cleaned)
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def _meaningful_lines(s: str) -> list[str]:
    lines = []
    for raw in _pty_plain_text(s).splitlines():
        line = raw.strip()
        if not line:
            continue
        if not _MEANINGFUL_LINE_RE.search(line):
            continue
        lower = line.lower()
        compact = re.sub(r"\s+", "", lower)
        if any(pat in lower for pat in _NOISE_PATTERNS):
            continue
        if any(pat in compact for pat in _NOISE_PATTERNS):
            continue
        if any(rx.search(compact) for rx in _NOISE_REGEXES):
            continue
        lines.append(line)
    return lines


def _decode(b: bytes | str) -> str:
    if isinstance(b, str):
        return b
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return repr(b)


def _find_claude_bin() -> Path:
    import shutil

    def _resolve_wrapper_target(path_str: str) -> Path | None:
        p = Path(path_str).resolve()
        if p.suffix.lower() in {".cmd", ".ps1"}:
            real = p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
            if real.exists():
                return real
        if p.suffix.lower() == ".exe" and p.exists():
            return p
        return None

    found = shutil.which("claude")
    if found:
        resolved = _resolve_wrapper_target(found)
        if resolved:
            return resolved

    candidates = [
        Path.home()
        / "AppData" / "Roaming" / "npm" / "node_modules"
        / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
    ]
    npm_prefix = os.environ.get("npm_config_prefix")
    if npm_prefix:
        candidates.append(
            Path(npm_prefix) / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        )
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


CLAUDE_BIN = _find_claude_bin()


def main() -> int:
    if not CLAUDE_BIN.exists():
        print(f"ERROR: claude.exe not found at {CLAUDE_BIN}", file=sys.stderr)
        return 2

    spawn_cwd = lc.resolve_spawn_cwd(CWD_ARG, ALIAS, backend_default=_HISTORICAL_CWD)
    ctx = lc.init_lifecycle(alias=ALIAS, cwd=spawn_cwd, backend="claude_code")
    log = ctx.log
    log.info("claude=%s trigger=%r settle=%ds", CLAUDE_BIN, TRIGGER_COMMAND, READY_SETTLE_SECS)

    pty_log_path = ctx.session_dir / "pty.log"
    pty_log = open(pty_log_path, "w", encoding="utf-8", errors="replace")
    outbox_path = ctx.session_dir / "outbox.txt"
    control_file = control_path(ALIAS)
    control_mode_file = control_mode_path(ALIAS)
    try:
        outbox_path.write_text("", encoding="utf-8")
    except Exception:
        pass

    notice_seq = 0
    last_relayed_block = ""
    pending_lines: list[str] = []
    last_pending_at = 0.0
    last_menu_payload = ""
    auto_wake_enabled = False
    last_auto_wake_at = 0.0
    loop_active = False

    def write_outbox_notice(text: str, *, icon: str = "!") -> None:
        nonlocal notice_seq
        notice_seq += 1
        stamp = datetime.now().strftime("%H:%M:%S")
        body = f"[{stamp}] {icon} {text}  (#{notice_seq})"
        try:
            outbox_path.write_text(f"[{stamp}]\n{body}\n", encoding="utf-8")
        except Exception:
            pass

    def set_control_mode(enabled: bool) -> None:
        try:
            if enabled:
                control_mode_file.write_text("menu", encoding="utf-8")
            elif control_mode_file.exists():
                control_mode_file.unlink()
        except Exception:
            pass

    def write_menu_block(block: str) -> None:
        nonlocal last_menu_payload
        menu = block.strip()
        extra = "\n8 auto wake loop\n9 send ESC\nreply digits like 234"
        payload = f"{menu}\n{extra}" if menu else extra.lstrip("\n")
        if payload == last_menu_payload:
            return
        last_menu_payload = payload
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            outbox_path.write_text(f"[{stamp}]\n{payload}\n", encoding="utf-8")
        except Exception:
            pass
        set_control_mode(True)

    def wake_loop() -> None:
        nonlocal last_auto_wake_at
        proc.write(TRIGGER_COMMAND + "\r")
        last_auto_wake_at = time.time()

    def ensure_loop_awake() -> None:
        if not loop_active:
            wake_loop()
            write_outbox_notice("tried to wake /chats-loop")
            time.sleep(0.5)

    def send_digit_sequence(seq: str) -> None:
        for ch in seq:
            proc.write(ch)
            proc.write("\r")
            time.sleep(0.15)

    def send_escape() -> None:
        proc.write("\x1b")

    def handle_control_input() -> None:
        nonlocal auto_wake_enabled
        if not control_file.exists():
            return
        try:
            command = control_file.read_text(encoding="utf-8").strip()
        except Exception:
            command = ""
        try:
            control_file.unlink()
        except Exception:
            pass
        if not command:
            return
        if command == "8":
            auto_wake_enabled = not auto_wake_enabled
            if auto_wake_enabled:
                ensure_loop_awake()
            state = "on" if auto_wake_enabled else "off"
            write_outbox_notice(f"auto wake loop {state}")
            return
        if command == "9":
            send_escape()
            write_outbox_notice("sent ESC")
            return
        ensure_loop_awake()
        send_digit_sequence(command)
        write_outbox_notice(f"sent option {command}")

    def flush_pending_pty(*, force: bool = False) -> None:
        nonlocal pending_lines, last_relayed_block, last_pending_at
        if not pending_lines:
            return
        if not force and (time.time() - last_pending_at) < 1.2:
            return
        block = "\n".join(pending_lines[-8:]).strip()
        pending_lines = []
        if not block or block == last_relayed_block:
            return
        last_relayed_block = block
        write_menu_block(block)

    def relay_pty_text(text: str) -> None:
        nonlocal pending_lines, last_pending_at
        lines = _meaningful_lines(text)
        if not lines:
            return
        for line in lines:
            if not pending_lines or pending_lines[-1] != line:
                pending_lines.append(line)
        last_pending_at = time.time()

    write_outbox_notice("starting Claude")
    set_control_mode(True)

    spawn_env = {**os.environ, "CHATS_LOOP_ALIAS": ALIAS}
    proc = PtyProcess.spawn(
        [str(CLAUDE_BIN), "--dangerously-skip-permissions"],
        dimensions=(40, 200),
        cwd=spawn_cwd,
        env=spawn_env,
    )
    log.info("spawned pid=%s", proc.pid)
    lc.write_meta(ctx, child_pid=proc.pid, trigger=TRIGGER_COMMAND)
    lc.record_spawned_child(ctx, proc.pid)

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
        set_control_mode(False)

    lc.install_cleanup(ctx, on_exit=_on_exit)

    import queue
    import threading

    read_q: queue.Queue[str | None] = queue.Queue()

    def _pty_reader() -> None:
        try:
            while proc.isalive():
                try:
                    chunk = proc.read(1024)
                    if chunk:
                        read_q.put(_decode(chunk))
                    else:
                        time.sleep(0.05)
                except EOFError:
                    break
                except Exception:
                    if not proc.isalive():
                        break
                    time.sleep(0.1)
        finally:
            read_q.put(None)

    def _control_watcher() -> None:
        while proc.isalive():
            try:
                handle_control_input()
            except Exception as e:
                log.warning("control watcher failed: %s", e)
            time.sleep(0.2)

    threading.Thread(target=_pty_reader, daemon=True).start()
    threading.Thread(target=_control_watcher, daemon=True).start()

    buffer = ""
    trust_dismissed = False
    trigger_sent = False
    tui_loaded = False
    last_output_at = time.time()

    while proc.isalive():
        try:
            text = read_q.get(timeout=0.5)
        except queue.Empty:
            flush_pending_pty()
            if not trigger_sent and tui_loaded and (time.time() - last_output_at >= READY_SETTLE_SECS):
                write_outbox_notice("activating chats-loop")
                try:
                    wake_loop()
                    trigger_sent = True
                    log.info("sent trigger: %r", TRIGGER_COMMAND)
                except Exception as e:
                    log.warning("trigger write failed: %s", e)
                    write_outbox_notice(f"trigger send failed: {e}", icon="x")
                last_output_at = time.time()
            continue

        if text is None:
            flush_pending_pty(force=True)
            break

        pty_log.write(text)
        pty_log.flush()
        relay_pty_text(text)
        buffer = (buffer + text)[-4096:]
        last_output_at = time.time()
        scan = _ansi_blind(buffer)

        if any(marker in scan for marker in _AUTH_ERROR_MARKERS):
            loop_active = False
            log.error("claude auth invalid during startup")
            write_outbox_notice("Claude Code auth invalid. Run /login in Claude Code and retry.", icon="!")
            return 1

        if "API Error" in scan or "ECONNRESET" in scan:
            loop_active = False

        if not tui_loaded and any(m in scan for m in _TUI_LOADED_MARKERS):
            tui_loaded = True
            write_outbox_notice("TUI loaded")

        if not trust_dismissed and "trustthisfolder" in scan.replace(" ", "").lower():
            try:
                proc.write("\r")
                trust_dismissed = True
                write_outbox_notice("accepted trust-folder prompt")
                buffer = ""
                last_output_at = time.time()
            except Exception as e:
                log.warning("trust-folder accept failed: %s", e)
            continue

        if trigger_sent and ("chats-loop loop active" in buffer or "loop active" in buffer):
            loop_active = True
            write_outbox_notice("session ready", icon="+")
            break

    if not proc.isalive():
        log.error("claude.exe died during startup")
        write_outbox_notice("Claude process exited during startup", icon="x")
        return 1

    set_control_mode(True)

    rate_limit_markers = ("You've hit your limit", "/rate-limit-options")
    permission_markers = ("Allowonce", "Allowforsession", "Allowalways", "allowthistool", "Allowtool")
    detect_window_bytes = 4096
    auth_error_reported = False
    last_press_3_at = 0.0
    recovery_cooldown_secs = 60
    pty_buffer = ""

    def press_3() -> None:
        nonlocal last_press_3_at
        try:
            proc.write("3\r")
            last_press_3_at = time.time()
            log.info("rate-limit dialog detected: sent '3\\r'")
        except Exception as e:
            log.warning("press 3 failed: %s", e)

    while proc.isalive():
        try:
            try:
                chunk = read_q.get(timeout=0.5)
            except queue.Empty:
                flush_pending_pty()
                if auto_wake_enabled and not loop_active:
                    now = time.time()
                    if now - last_auto_wake_at >= AUTO_WAKE_INTERVAL_SECS:
                        wake_loop()
                continue

            if chunk is None:
                break

            text = chunk
            pty_log.write(text)
            pty_log.flush()
            relay_pty_text(text)
            pty_buffer = (pty_buffer + text)[-detect_window_bytes:]
            scan_blind = _ansi_blind(pty_buffer)

            if not auth_error_reported and any(marker in scan_blind for marker in _AUTH_ERROR_MARKERS):
                auth_error_reported = True
                loop_active = False
                log.error("claude auth invalid during drain loop")
                write_outbox_notice("Claude Code auth invalid. Run /login in Claude Code.", icon="!")

            if "API Error" in scan_blind or "ECONNRESET" in scan_blind:
                loop_active = False

            if any(m in scan_blind for m in permission_markers):
                try:
                    proc.write("y")
                    write_outbox_notice("permission dialog detected; auto-accepted", icon="!")
                except Exception as e:
                    log.warning("permission auto-accept failed: %s", e)
                pty_buffer = ""

            if any(m in pty_buffer for m in rate_limit_markers):
                now = time.time()
                if now - last_press_3_at >= recovery_cooldown_secs:
                    press_3()
                    loop_active = False
                    write_outbox_notice("Claude account hit rate limit. Waiting for recovery.", icon="!")
                    pty_buffer = ""

            if "chats-loop loop active" in pty_buffer or "loop active" in pty_buffer:
                loop_active = True

            if auto_wake_enabled and not loop_active:
                now = time.time()
                if now - last_auto_wake_at >= AUTO_WAKE_INTERVAL_SECS:
                    wake_loop()

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.warning("drain read failed: %s", e)
            time.sleep(0.5)

    flush_pending_pty(force=True)
    set_control_mode(False)
    log.info("claude process exited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
