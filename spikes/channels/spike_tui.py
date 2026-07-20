"""Channels spike v4: 交互式 TUI 模式（决定 channels 方案生死）。

对比 v3（spike_run.py，-p stream-json）：本脚本用 winpty.PtyProcess 拉一个
**交互式** claude（无 -p），完全复刻 claude_code daemon 的托管形态，验证：
    外部 POST 通道事件 → 空闲的交互式会话被唤醒 → 调 reply 工具落盘 replies.log

判据：replies.log 出现内容 = TUI 模式通道可用 = channels 方案成立。
      超时无内容 = 不可用 = channels 对本项目不成立。

PTY 抓屏只用于两件事：①自动确认 dev-channel 全屏警告对话框；②判断会话
何时进入待命（出现 ready）以便喂初始消息。真正的成功判据 replies.log 是
out-of-band 的，不受 TUI 渲染脆弱性影响。
"""
import re
import sys
import threading
import time
import urllib.request
from pathlib import Path

try:
    from winpty import PtyProcess
except ImportError:
    print("ERROR: pywinpty not installed. Run: pip install pywinpty", file=sys.stderr)
    raise SystemExit(1)

HERE = Path(__file__).parent
REPLIES = HERE / "replies.log"
SCREEN = HERE / "tui-screen.log"
for f in (REPLIES, SCREEN):
    f.unlink(missing_ok=True)

# 交互式：无 -p、无 stream-json。其余 channel flag 与 v3 一致。
# 用环境变量做消融，免得反复改文件：
#   SPIKE_STRICT=0   去掉 --strict-mcp-config
#   SPIKE_ORDER=chan-first  把 dev-channel flag 放到 --mcp-config 之前
import os
_strict = os.environ.get("SPIKE_STRICT", "1") != "0"
_order = os.environ.get("SPIKE_ORDER", "mcp-first")

_mcp = ["--mcp-config", "./mcp-config.json"] + (["--strict-mcp-config"] if _strict else [])
_chan = ["--dangerously-load-development-channels", "server:wxchan"]
CMD = ["claude"] + (_chan + _mcp if _order == "chan-first" else _mcp + _chan) + [
    "--dangerously-skip-permissions",
]
log_variant = f"strict={_strict} order={_order}"

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB0]")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def clean(s):
    return ANSI.sub("", s)


def health_ok():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8791/health", timeout=2) as r:
            return r.read() == b"ok"
    except Exception:
        return False


log(f"spawning interactive claude via winpty ... [{log_variant}]")
log("  CMD: " + " ".join(CMD))
proc = PtyProcess.spawn(CMD, dimensions=(40, 200), cwd=str(HERE))
log(f"claude pid={proc.pid}")

screen = []              # 累积的干净屏文本
raw_lock = threading.Lock()
stop = threading.Event()


def reader():
    fh = SCREEN.open("a", encoding="utf-8")
    while not stop.is_set():
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
        text = clean(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace"))
        with raw_lock:
            screen.append(text)
        fh.write(text)
        fh.flush()
    fh.close()


threading.Thread(target=reader, daemon=True).start()


def screen_text():
    with raw_lock:
        return "".join(screen)


def wait_for(pat, timeout, label):
    """等屏上出现 pat（正则），返回 True/False。"""
    deadline = time.time() + timeout
    rx = re.compile(pat, re.I)
    while time.time() < deadline:
        if not proc.isalive():
            log(f"claude EXITED while waiting for {label}")
            return False
        if rx.search(screen_text()):
            return True
        time.sleep(0.5)
    return False


def send_line(text):
    proc.write(text)
    time.sleep(0.3)
    proc.write("\r")


# --- 阶段 1：处理 dev-channel 全屏警告对话框 ---
# 实测文案（winpty 会吞空格）：
#   WARNING:Loadingdevelopmentchannels ... Channels:server:wxchan
#   ❯1.Iamusingthisforlocaldevelopment / 2.Exit / Entertoconfirm
# 主动等对话框出现（首屏可能慢），出现后默认选项已是 1，直接 Enter 确认。
# 关键词用无空格容错正则（screen 已被 clean 去 ANSI，但空格被 TUI 渲染吞掉）。
if wait_for(r"development\s*channels?|Loadingdevelopment|localdevelopment", 20, "dev-channel warning dialog"):
    log("dev-channel warning dialog detected — pressing Enter to confirm option 1")
    log("  screen snippet: " + repr(screen_text()[-300:]))
    proc.write("\r")
    time.sleep(2)
    log("  after confirm: " + repr(screen_text()[-300:]))
else:
    log("no dev-channel warning dialog within 20s (maybe UX changed) — continuing")

# --- 阶段 2：等 MCP 通道就绪 + 会话可输入 ---
if not wait_for(r"wxchan|channel|>|▏|╰|Type|message", 25, "prompt ready"):
    log("prompt never looked ready — dumping last screen, aborting")
    log(repr(screen_text()[-800:]))
    stop.set(); proc.terminate(force=True); raise SystemExit(1)

log("prompt looks ready — sending initial message")
send_line("收到请只回复一个词：ready。之后保持待命，不要退出。")

# 等通道端口就绪（channel server 是 claude 的 stdio 子进程，claude 起来它就起来）
for _ in range(20):
    if health_ok():
        log("channel /health OK")
        break
    time.sleep(1)
else:
    log("channel never became healthy — abort")
    stop.set(); proc.terminate(force=True); raise SystemExit(1)

# 给会话一点时间答完初始轮
if wait_for(r"\bready\b", 30, "initial ready reply"):
    log("saw 'ready' — session is idle & standing by")
else:
    log("did not clearly see 'ready' (continuing anyway — TUI text is noisy)")

# --- 阶段 3：POST 通道事件，看空闲会话是否被唤醒 ---
log("POSTing channel event to idle session ...")
try:
    req = urllib.request.Request(
        "http://127.0.0.1:8791/",
        data="通道测试：请立即用 reply 工具回复文本『通道打通』，chat_id 用标签里的值。".encode("utf-8"),
        method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        log(f"POST -> {r.read().decode(errors='replace')}")
except Exception as e:
    log(f"POST failed: {e}")

# --- 阶段 4：等 reply 落盘（真正的判据）---
verdict = "UNKNOWN"
for i in range(45):  # ~90s
    if not proc.isalive():
        log(f"claude EXITED rc={proc.exitstatus} at ~{i*2}s")
        verdict = "FAIL (claude exited)"
        break
    if REPLIES.exists() and REPLIES.read_text(encoding="utf-8").strip():
        log("replies.log HAS CONTENT:")
        log(REPLIES.read_text(encoding="utf-8"))
        verdict = "PASS — TUI mode delivers channel events"
        break
    time.sleep(2)
else:
    log("no reply within ~90s")
    verdict = "FAIL — idle TUI session not woken by channel POST"

log(f"VERDICT: {verdict}")
log("cleanup: terminating claude")
stop.set()
try:
    proc.terminate(force=True)
except Exception:
    pass
