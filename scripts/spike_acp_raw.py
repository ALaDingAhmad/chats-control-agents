"""Raw ACP stdio dump: 完全绕过 SDK，直接收 hermes acp 进程的 jsonrpc 帧。

目的：仲裁"hermes 到底有没有发 session_update"。
SDK spike 一条 session_update 都没收到，但 hermes 日志说 response_len=25。
本脚本不用 SDK，直接 readline → 打印所有帧。

流程：
  1. initialize
  2. session/new (cwd=REPO)
  3. session/prompt (pwd)
  4. 期间 reader 把所有 hermes → 我们的帧全打出来，最后回应 session/request_permission

跑法：python scripts/spike_acp_raw.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STDERR_LOG = REPO / "scripts" / ".spike_raw.stderr.log"
CWD_A = str(REPO).replace("\\", "/")

PROMPT_TEXT = (
    "用 shell 工具执行 `pwd` 命令一次，"
    "然后只把那条命令的输出（一个绝对路径）原文回我，"
    "不要多余说明、不要 markdown、不要解释。"
)


class RawClient:
    def __init__(self):
        self.session_id: str | None = None
        self.session_ready = asyncio.Event()
        self.turn_done = asyncio.Event()
        self.session_updates: list[dict] = []  # 收到的所有 session_update.update 原文
        self.text_chunks: list[str] = []       # 累积出来的文本

    def on_response(self, obj: dict) -> None:
        """处理我们之前发的 id 请求的 response。"""
        rid = obj.get("id")
        result = obj.get("result")
        error = obj.get("error")
        if error:
            print(f"  ⚠ response id={rid} ERROR: {error}", flush=True)
            return
        if rid == 1:
            print(f"  ✓ initialize ok, agent={result.get('agentInfo')}", flush=True)
        elif rid == 2:
            self.session_id = result.get("sessionId")
            print(f"  ✓ session/new sessionId={self.session_id}", flush=True)
            self.session_ready.set()
        elif rid == 3:
            print(f"  ✓ session/prompt stopReason={result.get('stopReason')}", flush=True)
            self.turn_done.set()

    def on_notification_or_request(self, obj: dict) -> dict | None:
        """处理 hermes → 我们 的 method 调用。返回 dict 表示需要回应。"""
        method = obj.get("method")
        params = obj.get("params", {})
        rid = obj.get("id")

        if method == "session/update":
            update = params.get("update", {})
            su = update.get("sessionUpdate")
            self.session_updates.append(update)
            full = json.dumps(update, ensure_ascii=False)
            if len(full) > 250:
                full = full[:250] + "...<trunc>"
            print(f"  ← session/update[{su}] {full}", flush=True)
            # 累积文本（agent_message_chunk）
            if su == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    self.text_chunks.append(content.get("text", ""))
            return None  # session/update 是 notification，不需要回

        if method == "session/request_permission":
            # 自动批：选第一个 allow
            options = params.get("options", [])
            chosen = None
            for o in options:
                if "allow" in (o.get("optionId") or "").lower():
                    chosen = o.get("optionId")
                    break
            if chosen is None and options:
                chosen = options[0].get("optionId")
            print(f"  ← request_permission rid={rid} → allow {chosen!r}", flush=True)
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {"outcome": {"outcome": "selected", "optionId": chosen}},
            }

        # 其他 client capability: 我们不支持
        if rid is not None:
            print(f"  ← (unhandled request) method={method} rid={rid} — returning method_not_found", flush=True)
            return {
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": "Method not found"},
            }
        print(f"  ← (unhandled notif) method={method}", flush=True)
        return None


async def send(proc: asyncio.subprocess.Process, msg: dict) -> None:
    line = (json.dumps(msg) + "\n").encode("utf-8")
    proc.stdin.write(line)
    await proc.stdin.drain()
    preview = json.dumps(msg, ensure_ascii=False)
    if len(preview) > 200:
        preview = preview[:200] + "...<trunc>"
    print(f"→ {preview}", flush=True)


async def reader(proc: asyncio.subprocess.Process, client: RawClient) -> None:
    while True:
        line = await proc.stdout.readline()
        if not line:
            print("← EOF", flush=True)
            return
        s = line.decode("utf-8", errors="replace").strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except Exception as e:
            print(f"← (non-json line) {s!r} (err: {e})", flush=True)
            continue
        # response (有 id 没 method)
        if "result" in obj or "error" in obj:
            client.on_response(obj)
            continue
        # notification / request (有 method)
        if "method" in obj:
            resp = client.on_notification_or_request(obj)
            if resp is not None:
                await send(proc, resp)
            continue
        print(f"← (unknown frame) {s[:200]}", flush=True)


async def main() -> int:
    stderr_fh = open(STDERR_LOG, "w", encoding="utf-8", errors="replace")
    proc = await asyncio.create_subprocess_exec(
        "hermes", "acp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr_fh,
        env={**os.environ},
    )
    print(f"[raw] pid={proc.pid}, stderr→{STDERR_LOG}", flush=True)
    print(f"[raw] CWD_A={CWD_A}", flush=True)

    client = RawClient()
    rd = asyncio.create_task(reader(proc, client))

    # Step 1: initialize
    await send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
            },
        },
    })
    await asyncio.sleep(2)

    # Step 2: new_session
    await send(proc, {
        "jsonrpc": "2.0", "id": 2, "method": "session/new",
        "params": {"cwd": CWD_A, "mcpServers": []},
    })
    try:
        await asyncio.wait_for(client.session_ready.wait(), timeout=90)
    except asyncio.TimeoutError:
        print("[raw] FAIL: session/new timeout after 90s", flush=True)
        proc.terminate(); rd.cancel(); return 1

    # Step 3: prompt
    await send(proc, {
        "jsonrpc": "2.0", "id": 3, "method": "session/prompt",
        "params": {
            "sessionId": client.session_id,
            "prompt": [{"type": "text", "text": PROMPT_TEXT}],
        },
    })
    try:
        await asyncio.wait_for(client.turn_done.wait(), timeout=120)
    except asyncio.TimeoutError:
        print("[raw] FAIL: prompt turn timeout", flush=True)

    # 给 reader 一点时间把可能还没读完的尾巴拉完
    await asyncio.sleep(2)

    print("\n=== 总结 ===", flush=True)
    print(f"session_updates 收到条数: {len(client.session_updates)}", flush=True)
    by_kind: dict[str, int] = {}
    for u in client.session_updates:
        k = u.get("sessionUpdate", "?")
        by_kind[k] = by_kind.get(k, 0) + 1
    for k, n in by_kind.items():
        print(f"  {k}: {n}", flush=True)
    text = "".join(client.text_chunks)
    print(f"累积文本（{len(text)} chars）: {text!r}", flush=True)

    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), 5)
    except Exception:
        proc.kill()
    rd.cancel()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
