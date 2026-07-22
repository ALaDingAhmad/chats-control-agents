"""claude_channel backend daemon —— Claude Code channels（推模型）。

契约见 docs/后端设计.md「claude_channel backend 契约」。

daemon 职责（路径外站位 + 内部 daemon↔channel_server 分工）：
  1. winpty 拉交互式 claude（**不带 --strict-mcp-config**，带 dev-channel flag），
     spawn 后自动喂 \\r 确认 dev-channel 全屏警告框。
  2. 起本地 HTTP 回调服务（/reply）：channel_server 收到 claude 的 reply 工具调用
     后 POST 过来 → daemon 覆写 outbox.txt（复用 _write_outbox，格式同 mcp_bridge）。
  3. poll inbox.txt（mtime 判新 + startup baseline 防重放）→ 有新消息就 POST 到
     channel_server 的 /inject → notification 推进会话。
  4. 写 ready marker（~/.claude/.chats-loop-active-<alias>）让 web spawn.watch_ready
     识别就绪，跟 claude_code / hermes_acp 共用同一约定。

CLI: python -m chats_control_agents.backends.claude_channel.daemon [<alias>] [<cwd>]
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from chats_control_agents.core import daemon_lifecycle as lc
from chats_control_agents.core.paths import ROOT, control_path, inbox_path, outbox_path
from chats_control_agents.core.resume_scan import tail_turns
# 复用 claude_code daemon 的 claude.exe 定位（纯函数，无副作用）
from chats_control_agents.backends.claude_code.daemon import _find_claude_bin

try:
    from winpty import PtyProcess
except ImportError:
    print("ERROR: pywinpty not installed. Run: pip install pywinpty", file=sys.stderr)
    sys.exit(2)

BACKEND = "claude_channel"
CHANNEL_NAME = "wxchan"

# inbox 轮询间隔 —— 跟 claude_code / hermes 一致
POLL_INTERVAL_SECS = 0.5

# ready marker：跟 claude_code / hermes 共用同一组目录约定
_MARKER_DIR = Path.home() / ".claude"

# dev-channel 警告框 / TUI 就绪的抓屏 marker（实测见 CHANNELS预研.md）
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB0]")
_WARNING_MARKERS = ("development", "localdevelopment", "Loadingdevelopment")
_TUI_READY_MARKERS = ("bypass", "Try", "effort")

CLAUDE_BIN = _find_claude_bin()
CHANNEL_SERVER = Path(__file__).with_name("channel_server.mjs")


def _render_mcp_config(ctx) -> Path:
    """把 channel_server 的绝对路径渲染进 session 目录下的 mcp-config.json。
    不把机器绝对路径写死进仓库——每次启动按本机路径生成。
    """
    cfg = {
        "mcpServers": {
            CHANNEL_NAME: {
                "command": "node",
                "args": [str(CHANNEL_SERVER)],
            }
        }
    }
    out = ctx.session_dir / "channel-mcp-config.json"
    out.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return out

# CLI
ALIAS, CWD_ARG = lc.parse_cli_args(default_cwd=Path.home())


def _marker_path(alias: str) -> Path:
    return _MARKER_DIR / f".chats-loop-active-{alias}"


def _write_outbox(alias: str, text: str) -> None:
    """跟 claude_code mcp_bridge / hermes 一致的 outbox 格式：覆写 `[HH:MM:SS]\\n<reply>\\n`。"""
    stamp = datetime.now().strftime("%H:%M:%S")
    p = outbox_path(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"[{stamp}]\n{text}\n", encoding="utf-8")


def _resume_recap(cwd: str, session_id: str) -> str:
    """接回后给微信看的"最近两轮对话"文本；取不到返回 ''（调用方跳过）。

    数据源是 transcript（含 claude 回复），见 resume_scan.tail_turns +
    docs/入站路由.md "接回后回顾"。任何异常都吞掉——回顾是锦上添花，
    绝不能因它挂掉 resume 就绪流程。
    """
    try:
        pairs = tail_turns(cwd, session_id)
    except Exception:
        return ""
    if not pairs:
        return ""
    blocks = ["📜 上次聊到这里："]
    for p in pairs:
        if p.get("user"):
            blocks.append(f"你：{p['user']}")
        if p.get("assistant"):
            blocks.append(f"我：{p['assistant']}")
    return "\n".join(blocks)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _clean(s: str) -> str:
    return _ANSI.sub("", s)


def _start_reply_server(alias: str, log) -> int:
    """起本地 HTTP 服务收 channel_server 的 /reply 回调；返回监听端口。"""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/reply":
                self.send_response(404); self.end_headers(); return
            try:
                n = int(self.headers.get("content-length", 0))
                body = self.rfile.read(n).decode("utf-8")
                data = json.loads(body)
                text = str(data.get("text", "")).strip()
            except Exception as e:
                log.warning("reply callback parse failed: %s", e)
                self.send_response(400); self.end_headers(); return
            if text:
                _write_outbox(alias, text)
                log.info("reply → outbox (%d chars)", len(text))
            self.send_response(200); self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):  # 静音默认 stderr 日志
            pass

    port = _free_port()
    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("reply callback server on 127.0.0.1:%d", port)
    return port


def _inject(inject_port: int, text: str, log) -> None:
    """把 inbox 新消息 POST 到 channel_server 的 /inject。"""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{inject_port}/inject",
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except Exception as e:
        log.warning("inject POST failed: %s", e)


def _inject_health(inject_port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{inject_port}/health", timeout=2) as r:
            return r.read() == b"ok"
    except Exception:
        return False


def _spawn_and_wait_ready(ctx, log, mcp_config, spawn_cwd, spawn_env, inject_port,
                          resume_session_id: str | None = None):
    """Spawn child claude (optionally with --resume) and drive it to ready.

    Encapsulates: build cmd → PtyProcess.spawn → PTY reader thread → confirm the
    dev-channel warning dialog → wait for channel_server /health. Returns
    (proc, ok). ok=False means the session never became healthy (caller decides
    whether to fall back). Each call owns its own reader thread + screen buffer,
    so resume re-spawns don't tangle with the previous child's I/O.

    resume_session_id: when set, adds `--resume <id>` so claude restores that
    transcript's context. Verified compatible with the dev-channel flag
    (docs/后端设计.md "resume 控制通路"); the warning-dialog step is unchanged.
    """
    cmd = [str(CLAUDE_BIN), "--mcp-config", str(mcp_config)]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd += [
        "--dangerously-load-development-channels", f"server:{CHANNEL_NAME}",
        "--dangerously-skip-permissions",
    ]
    log.info("spawn cmd%s: %s",
             " (resume)" if resume_session_id else "", " ".join(cmd))
    proc = PtyProcess.spawn(cmd, dimensions=(40, 200), cwd=spawn_cwd, env=spawn_env)
    log.info("spawned claude pid=%s", proc.pid)

    # PTY reader: accumulate screen text for dialog-confirm + ready detection.
    screen: list[str] = []
    lock = threading.Lock()
    pty_log = ctx.session_dir / "pty.log"

    def _reader() -> None:
        fh = pty_log.open("a", encoding="utf-8")
        while proc.isalive():
            try:
                chunk = proc.read(2048)
            except EOFError:
                break
            except Exception:
                if not proc.isalive():
                    break
                time.sleep(0.1)
                continue
            if not chunk:
                time.sleep(0.05)
                continue
            text = _clean(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
            with lock:
                screen.append(text)
            try:
                fh.write(text)
                fh.flush()
            except Exception:
                pass
        fh.close()

    threading.Thread(target=_reader, daemon=True).start()

    def _screen() -> str:
        with lock:
            return "".join(screen)

    # 阶段 1：等 dev-channel 警告框 → 喂 \r 确认选项 1（local development）。
    warned = False
    deadline = time.time() + 20
    while time.time() < deadline:
        if not proc.isalive():
            log.error("claude exited before warning dialog")
            return proc, False
        if any(m.lower() in _screen().lower().replace(" ", "") for m in _WARNING_MARKERS):
            log.info("dev-channel warning dialog detected — confirming")
            proc.write("\r")
            warned = True
            time.sleep(2)
            break
        time.sleep(0.5)
    if not warned:
        log.warning("no dev-channel warning dialog within 20s (continuing)")

    # 阶段 2：等 channel_server /health OK。
    for _ in range(30):
        if _inject_health(inject_port):
            log.info("channel_server /health OK on inject_port=%d", inject_port)
            return proc, True
        if not proc.isalive():
            log.error("claude exited before channel healthy")
            return proc, False
        time.sleep(1)
    log.error("channel_server never became healthy")
    return proc, False


def main() -> int:
    if not CLAUDE_BIN.exists():
        print(f"ERROR: claude.exe not found at {CLAUDE_BIN}", file=sys.stderr)
        return 2
    if not CHANNEL_SERVER.exists():
        print(f"ERROR: channel_server.mjs missing at {CHANNEL_SERVER}", file=sys.stderr)
        return 2

    try:
        spawn_cwd = lc.resolve_spawn_cwd(CWD_ARG, ALIAS)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        try:
            p = outbox_path(ALIAS)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"⚠️ 项目目录不存在，无法启动。请用 /new 重新创建会话。\n{e}\n", encoding="utf-8")
        except Exception:
            pass
        return 3

    ctx = lc.init_lifecycle(alias=ALIAS, cwd=spawn_cwd, backend=BACKEND)
    log = ctx.log
    log.info("claude=%s channel_server=%s", CLAUDE_BIN, CHANNEL_SERVER)
    mcp_config = _render_mcp_config(ctx)

    # 端口分配：inject 端口给 channel_server 监听；reply 端口给 daemon 回调服务。
    inject_port = _free_port()
    reply_port = _start_reply_server(ALIAS, log)

    # spawn 交互式 claude。channel_server 的两个端口 + 通道名通过 env 传入，
    # child claude 继承，spawn channel_server 子进程时再传下去。
    # 死记：不带 --strict-mcp-config（会屏蔽 dev channel 注册，见 CHANNELS预研.md）。
    spawn_env = {
        **os.environ,
        "CHANNEL_INJECT_PORT": str(inject_port),
        "CHANNEL_REPLY_URL": f"http://127.0.0.1:{reply_port}/reply",
        "CHANNEL_NAME": CHANNEL_NAME,
    }
    # proc 用可变 holder 持有，resume 重启时原地替换（_on_exit / 主循环都读它）。
    holder: dict[str, object] = {"proc": None}

    proc, ok = _spawn_and_wait_ready(
        ctx, log, mcp_config, spawn_cwd, spawn_env, inject_port)
    holder["proc"] = proc
    lc.write_meta(ctx, child_pid=proc.pid)
    lc.record_spawned_child(ctx, proc.pid)

    def _on_exit() -> None:
        cur = holder.get("proc")
        try:
            if cur is not None and cur.isalive():
                cur.terminate(force=True)
                log.info("cleanup: killed claude pid=%s", cur.pid)
        except Exception as e:
            log.warning("cleanup kill failed: %s", e)
        try:
            _marker_path(ALIAS).unlink(missing_ok=True)
        except Exception:
            pass

    lc.install_cleanup(ctx, on_exit=_on_exit)

    if not ok:
        log.error("initial spawn never became ready")
        return 5

    def _do_resume(session_id: str) -> None:
        """Kill current child and re-spawn with --resume <session_id>.

        See docs/后端设计.md "resume 控制通路". On failure (e.g. transcript not
        found → claude exits) fall back: tell the user, keep the daemon alive so
        a fresh /proj can recover. The old child is always killed first — a
        session has at most one live child (single-select model).
        """
        old = holder.get("proc")
        log.info("RESUME requested → session=%s (killing pid=%s)",
                 session_id, getattr(old, "pid", "?"))
        try:
            if old is not None and old.isalive():
                old.terminate(force=True)
        except Exception as e:
            log.warning("resume: kill old child failed: %s", e)
        new_proc, ok2 = _spawn_and_wait_ready(
            ctx, log, mcp_config, spawn_cwd, spawn_env, inject_port,
            resume_session_id=session_id)
        holder["proc"] = new_proc
        if ok2:
            lc.write_meta(ctx, child_pid=new_proc.pid)
            lc.record_spawned_child(ctx, new_proc.pid)
            log.info("resume ready, new pid=%s", new_proc.pid)
            # 推最近两轮对话让用户看到"上次聊到哪"（child claude 内存里有
            # 历史但不会主动打印）+ 接回确认。**必须拼成一条** _write_outbox：
            # outbox 只保留最新一条（覆写语义，见 CLAUDE.md），分两次写后一条
            # 会盖掉前一条。取不到回顾就只发确认。见 docs/入站路由.md "接回后回顾"。
            recap = _resume_recap(spawn_cwd, session_id)
            tail = "✅ 已接回历史会话，可以继续对话了。"
            _write_outbox(ALIAS, f"{recap}\n\n{tail}" if recap else tail)
        else:
            log.error("resume spawn never became ready (session=%s)", session_id)
            _write_outbox(
                ALIAS,
                "⚠️ 接回该会话失败（可能历史已失效）。已保持在线，"
                "发消息可继续，或用 /proj 重新选。",
            )

    def _check_resume_signal() -> bool:
        """Read+delete control_path if it carries a RESUME: signal.

        Returns True if a resume was handled this tick. Non-RESUME content is
        ignored (claude_channel doesn't do PTY control) but still consumed so it
        can't linger. See docs/入站路由.md.
        """
        cp = control_path(ALIAS)
        if not cp.exists():
            return False
        try:
            raw = cp.read_text(encoding="utf-8").strip()
        except Exception:
            raw = ""
        try:
            cp.unlink()
        except Exception:
            pass
        if raw.startswith("RESUME:"):
            sid = raw[len("RESUME:"):].strip()
            if sid:
                _do_resume(sid)
                return True
        return False

    # 就绪：写 marker，让 web/spawn.watch_ready 给用户发就绪通知。
    try:
        _marker_path(ALIAS).write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        log.warning("write ready marker failed: %s", e)
    print(f"[daemon] ready, polling inbox for alias={ALIAS}")
    log.info("entering inbox poll loop")

    # 阶段 3：inbox 轮询主循环。startup 以当前 mtime 为 baseline，不重放旧消息。
    p = inbox_path(ALIAS)
    last_mtime = p.stat().st_mtime if p.exists() else 0.0
    while True:
        # resume 控制信号优先于 inbox：若本轮做了 resume，child 已换新，
        # inbox baseline 不动（resume 不消费用户消息，只换会话）。
        if _check_resume_signal():
            proc = holder["proc"]  # type: ignore[assignment]
            continue
        proc = holder["proc"]  # type: ignore[assignment]
        if proc is None or not proc.isalive():
            log.error("claude died — exiting daemon")
            _write_outbox(ALIAS, "⚠️ 会话进程已退出（可能撞限额或崩溃）。请用 /new 重开。")
            return 6
        try:
            if p.exists():
                mt = p.stat().st_mtime
                if mt > last_mtime:
                    last_mtime = mt
                    text = p.read_text(encoding="utf-8").strip()
                    if text:
                        log.info("inbox new msg (%d chars): %r", len(text), text[:120])
                        _inject(inject_port, text, log)
        except Exception as e:
            log.warning("inbox poll error: %s", e)
        time.sleep(POLL_INTERVAL_SECS)


if __name__ == "__main__":
    sys.exit(main())
