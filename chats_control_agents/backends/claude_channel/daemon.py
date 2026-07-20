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
from chats_control_agents.core.paths import ROOT, inbox_path, outbox_path
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
    cmd = [
        str(CLAUDE_BIN),
        "--mcp-config", str(mcp_config),
        "--dangerously-load-development-channels", f"server:{CHANNEL_NAME}",
        "--dangerously-skip-permissions",
    ]
    log.info("spawn cmd: %s", " ".join(cmd))
    proc = PtyProcess.spawn(cmd, dimensions=(40, 200), cwd=spawn_cwd, env=spawn_env)
    log.info("spawned claude pid=%s", proc.pid)
    lc.write_meta(ctx, child_pid=proc.pid)
    lc.record_spawned_child(ctx, proc.pid)

    def _on_exit() -> None:
        try:
            if proc.isalive():
                proc.terminate(force=True)
                log.info("cleanup: killed claude pid=%s", proc.pid)
        except Exception as e:
            log.warning("cleanup kill failed: %s", e)
        try:
            _marker_path(ALIAS).unlink(missing_ok=True)
        except Exception:
            pass

    lc.install_cleanup(ctx, on_exit=_on_exit)

    # PTY 读线程：累积屏文本，用于确认警告框 + 判就绪。
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

    # 阶段 1：等 dev-channel 警告框出现 → 喂 \r 确认选项 1（local development）。
    warned = False
    deadline = time.time() + 20
    while time.time() < deadline:
        if not proc.isalive():
            log.error("claude exited before warning dialog")
            return 4
        if any(m.lower() in _screen().lower().replace(" ", "") for m in _WARNING_MARKERS):
            log.info("dev-channel warning dialog detected — confirming")
            proc.write("\r")
            warned = True
            time.sleep(2)
            break
        time.sleep(0.5)
    if not warned:
        log.warning("no dev-channel warning dialog within 20s (continuing)")

    # 阶段 2：等 channel_server /health OK（channel_server 是 claude 的 stdio 子进程，
    # claude 起来它就起来）。
    for _ in range(30):
        if _inject_health(inject_port):
            log.info("channel_server /health OK on inject_port=%d", inject_port)
            break
        if not proc.isalive():
            log.error("claude exited before channel healthy")
            return 4
        time.sleep(1)
    else:
        log.error("channel_server never became healthy")
        return 5

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
        if not proc.isalive():
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
