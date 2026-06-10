"""Spike: 验证 hermes ACP 是否能作为 agent-bridge 的 backend 通道。

验证三件事：
  1. initialize 握手能通
  2. 单 session: new_session(cwd=A) + prompt("pwd") → 回复命中 A
  3. 并发隔离: 同进程开两 session 各自 cwd，并发各发 pwd，回复各命中自己 cwd

跑法：
  python scripts/spike_hermes_acp.py

stderr 落盘：scripts/.spike_acp.stderr.log
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from acp import (
    PROTOCOL_VERSION,
    Client,
    RequestPermissionResponse,
    connect_to_agent,
)
from acp.schema import AllowedOutcome, TextContentBlock

REPO = Path(__file__).resolve().parents[1]
HERMES = Path("F:/wslshare/hermes-agent")
STDERR_LOG = REPO / "scripts" / ".spike_acp.stderr.log"

CWD_A = str(REPO).replace("\\", "/")
CWD_B = str(HERMES).replace("\\", "/")

PROMPT_TEXT = (
    "用 shell 工具执行 `pwd` 命令一次，"
    "然后只把那条命令的输出（一个绝对路径）原文回我，"
    "不要多余说明、不要 markdown、不要解释。"
)


class SpikeClient(Client):
    """收事件 + 自动批工具审批。"""

    def __init__(self):
        self.buffers: dict[str, list[str]] = {}

    async def session_update(self, params) -> None:
        sid = params.session_id
        update = params.update
        kind = type(update).__name__
        # 把整条事件 dump 出来（截断），便于看清楚 hermes 到底发了什么字段
        try:
            raw = update.model_dump(by_alias=False, exclude_none=True)
        except Exception:
            raw = str(update)
        raw_str = repr(raw)
        if len(raw_str) > 400:
            raw_str = raw_str[:400] + "...<trunc>"
        print(f"[{sid[:8]}/{kind}] {raw_str}", flush=True)
        # 文本累计（AgentMessageChunk 里 content 是单个 block；
        # AgentResponseMessage 没有但也不会出现在 session_update 里）
        content = getattr(update, "content", None)
        text = getattr(content, "text", None) if content is not None else None
        if text and kind == "AgentMessageChunk":
            self.buffers.setdefault(sid, []).append(text)

    async def request_permission(self, params) -> RequestPermissionResponse:
        # 选第一个 allow_* 选项
        chosen = None
        for o in params.options:
            oid = (o.option_id or "").lower()
            if "allow" in oid:
                chosen = o.option_id
                break
        if chosen is None and params.options:
            chosen = params.options[0].option_id
        print(f"[perm] auto-allow option_id={chosen}", flush=True)
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=chosen)
        )

    async def write_text_file(self, params): return None
    async def read_text_file(self, params): return None
    async def create_terminal(self, params): return None
    async def terminal_output(self, params): return None
    async def release_terminal(self, params): return None
    async def wait_for_terminal_exit(self, params): return None
    async def kill_terminal(self, params): return None


async def spawn_acp() -> asyncio.subprocess.Process:
    """走全局 `hermes acp`（不依赖 hermes 源码 cwd）。"""
    STDERR_LOG.parent.mkdir(exist_ok=True)
    stderr_fh = open(STDERR_LOG, "w", encoding="utf-8", errors="replace")
    proc = await asyncio.create_subprocess_exec(
        "hermes", "acp",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr_fh,
        env={**os.environ},
    )
    print(f"[spike] ACP server pid={proc.pid}, stderr → {STDERR_LOG}", flush=True)
    return proc


async def run_turn(conn, client: SpikeClient, sid: str, tag: str) -> str:
    client.buffers[sid] = []
    print(f"[{tag}/{sid[:8]}] prompt.submit ...", flush=True)
    resp = await conn.prompt(
        prompt=[TextContentBlock(type="text", text=PROMPT_TEXT)],
        session_id=sid,
    )
    text = "".join(client.buffers.get(sid, [])).strip()
    print(f"[{tag}/{sid[:8]}] stop={resp.stop_reason} final={text!r}", flush=True)
    return text


def norm(s: str) -> str:
    return s.replace("/", "").replace("\\", "").lower()


async def main() -> int:
    print(f"[spike] CWD_A={CWD_A}", flush=True)
    print(f"[spike] CWD_B={CWD_B}", flush=True)

    proc = await spawn_acp()
    client = SpikeClient()
    conn = connect_to_agent(
        client,
        input_stream=proc.stdin,     # 我们写 → agent 读
        output_stream=proc.stdout,   # agent 写 → 我们读
        use_unstable_protocol=True,  # 跟 server 端 entry.py 一致
    )

    print("\n=== Step 1: initialize ===", flush=True)
    init = await conn.initialize(protocol_version=PROTOCOL_VERSION)
    print(f"[spike] protocol_version={init.protocol_version}", flush=True)
    print(f"[spike] agent_info={getattr(init,'agent_info',None)}", flush=True)
    print(f"[spike] agent_capabilities={getattr(init,'agent_capabilities',None)}", flush=True)

    print("\n=== Step 2: 单 session pwd (CWD_A) ===", flush=True)
    ns_a = await conn.new_session(cwd=CWD_A, mcp_servers=None)
    out_a = await run_turn(conn, client, ns_a.session_id, "A")
    hit_a = norm(CWD_A) in norm(out_a)

    print("\n=== Step 3: 并发两 session 各自 cwd ===", flush=True)
    ns_a2 = await conn.new_session(cwd=CWD_A, mcp_servers=None)
    ns_b = await conn.new_session(cwd=CWD_B, mcp_servers=None)
    out_a2, out_b = await asyncio.gather(
        run_turn(conn, client, ns_a2.session_id, "A2"),
        run_turn(conn, client, ns_b.session_id, "B"),
    )
    hit_a2 = norm(CWD_A) in norm(out_a2)
    hit_b = norm(CWD_B) in norm(out_b)

    print("\n=== 结果 ===", flush=True)
    print(f"  Step1 握手:                {'✅' if init.protocol_version else '❌'}", flush=True)
    print(f"  Step2 单session cwd 命中:  {'✅' if hit_a else '❌'}", flush=True)
    print(f"  Step3 并发隔离 A2:         {'✅' if hit_a2 else '❌'}", flush=True)
    print(f"  Step3 并发隔离 B:          {'✅' if hit_b else '❌'}", flush=True)

    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        proc.kill()
    return 0 if (hit_a and hit_a2 and hit_b) else 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(asyncio.wait_for(main(), timeout=180))
    except asyncio.TimeoutError:
        print("[spike] TIMEOUT after 180s", file=sys.stderr)
        rc = 2
    sys.exit(rc)
