"""Channels spike feeder v3: 一个进程内闭环。
1. 启动无头 claude（stream-json, stdin 保持打开）。
2. 发初始消息 → 等到看见第一个 result（会话完成一轮）。
3. 轮询 wxchan /health 直到通道端口就绪（解决 startup race）。
4. **自己 POST** 一条通道消息 → 观察会话是否被唤醒、是否调 reply。
5. 保持存活直到 replies.log 出现或超时，然后干净退出。
所有阶段打时间戳，stdout 落 claude-out.log，本脚本日志走 stderr/print。"""
import json
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
REPLIES = HERE / "replies.log"
for f in (REPLIES, HERE / "claude-out.log"):
    f.unlink(missing_ok=True)

CMD = (
    "claude -p --input-format stream-json --output-format stream-json --verbose "
    "--mcp-config ./mcp-config.json --strict-mcp-config "
    "--dangerously-load-development-channels server:wxchan "
    "--dangerously-skip-permissions"
)

out = open(HERE / "claude-out.log", "wb")
p = subprocess.Popen(CMD, shell=True, cwd=str(HERE),
                     stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT)


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def health_ok():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8791/health", timeout=2) as r:
            return r.read() == b"ok"
    except Exception:
        return False


def send_user(text):
    msg = {"type": "user", "message": {"role": "user",
           "content": [{"type": "text", "text": text}]}}
    p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    p.stdin.flush()


log(f"claude pid={p.pid}")
send_user("收到请只回复一个词：ready。之后保持待命。")
log("initial message sent")

# 等通道端口就绪
for _ in range(20):
    if health_ok():
        log("channel /health OK")
        break
    time.sleep(1)
else:
    log("channel never became healthy — abort")
    p.kill(); raise SystemExit(1)

# POST 通道消息
log("POSTing channel event ...")
try:
    req = urllib.request.Request(
        "http://127.0.0.1:8791/",
        data="通道测试：请立即用 reply 工具回复文本『通道打通』，chat_id 用标签里的值。".encode("utf-8"),
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        log(f"POST -> {r.read().decode(errors='replace')}")
except Exception as e:
    log(f"POST failed: {e}")

# 等 reply 落盘
for i in range(40):
    if p.poll() is not None:
        log(f"claude EXITED rc={p.returncode} at {i}s")
        break
    if REPLIES.exists() and REPLIES.read_text(encoding="utf-8").strip():
        log("replies.log HAS CONTENT:")
        log(REPLIES.read_text(encoding="utf-8"))
        break
    time.sleep(2)
else:
    log("no reply within 80s")

log("cleanup: killing claude")
try:
    p.kill()
except Exception:
    pass
