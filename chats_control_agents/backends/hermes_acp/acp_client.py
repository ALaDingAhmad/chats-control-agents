"""Hermes ACP stdio JSON-RPC client。

Daemon-agnostic：本模块只关心 ACP 协议（spawn `hermes acp` 子进程、收发
newline-delimited JSON 帧、聚合 `session/update` 事件、自动批 `session/request_permission`）。
跟 chat_sessions 文件协议、生命周期、inbox/outbox 全部解耦。

设计依据：`scripts/spike_acp_raw.py`（实测验证过的蓝本），具体协议事实
见 `docs/HERMES-ACP-SPIKE.md`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional


log = logging.getLogger("hermes_acp.client")


# ── 默认超时 ──────────────────────────────────────────────────────────────
# 首次 session/new 要 60-80s（LexAI client init + vision probe），给到 120s
# 留余量。后续 session/new 1-2s 内就回，对它们冗余开销可忽略。
DEFAULT_NEW_SESSION_TIMEOUT = 120.0
# 单个 prompt turn 的等待：含 LLM 推理 + 工具调用循环。给 5 分钟兜底。
DEFAULT_PROMPT_TIMEOUT = 300.0
# initialize 应该秒回。
DEFAULT_INIT_TIMEOUT = 30.0


@dataclass
class TurnResult:
    """一次 `prompt` 调用的最终产物。"""
    stop_reason: str           # "end_turn" / "cancelled" / "max_tokens" / …
    text: str                  # 聚合后的最终回复（agent_message_chunk 拼接）
    thoughts: str = ""         # 聚合后的思考流（agent_thought_chunk 拼接），仅日志
    tool_calls: int = 0        # 工具调用次数（仅观测，不参与回复）
    usage: dict = field(default_factory=dict)  # 最后一次 usage_update 快照


class AcpClient:
    """A long-lived stdio JSON-RPC client to a single `hermes acp` subprocess.

    用法：
        client = AcpClient(stderr_log_path="…/hermes_stderr.log")
        await client.start()
        await client.initialize()
        session_id = await client.new_session(cwd="…")
        result = await client.prompt(session_id, "用户消息")
        # result.text 是最终回复
        await client.stop()
    """

    def __init__(self, stderr_log_path: Optional[str] = None, hermes_cmd: tuple[str, ...] = ("hermes", "acp")):
        self._cmd = hermes_cmd
        self._stderr_log_path = stderr_log_path
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_fh = None

        # 请求-响应配对：rid → Future
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}

        # 当前活跃 turn 的事件聚合（同一进程只允许串行 turn —— hermes 在
        # session 内本来就是串行的，我们这边一对一映射就够）
        self._turn_session_id: Optional[str] = None
        self._turn_text_chunks: list[str] = []
        self._turn_thought_chunks: list[str] = []
        self._turn_tool_calls: int = 0
        self._turn_last_usage: dict = {}

    # ── 生命周期 ─────────────────────────────────────────────────────────
    async def start(self) -> int:
        """Spawn `hermes acp` 子进程。返回 child PID。"""
        if self._stderr_log_path:
            self._stderr_fh = open(self._stderr_log_path, "w", encoding="utf-8", errors="replace")
            stderr = self._stderr_fh
        else:
            stderr = asyncio.subprocess.DEVNULL

        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            env={**os.environ},
        )
        log.info("hermes acp spawned pid=%s cmd=%s", self._proc.pid, self._cmd)
        self._reader_task = asyncio.create_task(self._reader_loop(), name="acp.reader")
        return self._proc.pid

    async def stop(self) -> None:
        """Terminate 子进程 + 取消 reader。多次调用安全。"""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
            except ProcessLookupError:
                pass
        if self._stderr_fh:
            try:
                self._stderr_fh.close()
            except Exception:
                pass
        # 失败所有 pending（让上游不要永远卡着）
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("acp client stopped"))
        self._pending.clear()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def child_pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    # ── 公开方法 ─────────────────────────────────────────────────────────
    async def initialize(self, timeout: float = DEFAULT_INIT_TIMEOUT) -> dict:
        """ACP initialize 握手。返回 agent info / capabilities 字典。"""
        return await self._request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            },
            timeout=timeout,
        )

    async def new_session(self, cwd: str, timeout: float = DEFAULT_NEW_SESSION_TIMEOUT) -> str:
        """新建一个 ACP session。返回 sessionId。"""
        result = await self._request(
            "session/new",
            {"cwd": cwd, "mcpServers": []},
            timeout=timeout,
        )
        sid = result.get("sessionId")
        if not sid:
            raise RuntimeError(f"session/new response missing sessionId: {result!r}")
        return sid

    async def prompt(
        self,
        session_id: str,
        text: str,
        timeout: float = DEFAULT_PROMPT_TIMEOUT,
    ) -> TurnResult:
        """Send a user prompt and aggregate events until the turn ends.

        发起前会重置 turn 聚合 buffer——同一时刻只能跑一个 turn（hermes session
        语义就是串行）。
        """
        if self._turn_session_id is not None:
            raise RuntimeError(f"another turn is in flight (session_id={self._turn_session_id})")
        self._turn_session_id = session_id
        self._turn_text_chunks = []
        self._turn_thought_chunks = []
        self._turn_tool_calls = 0
        self._turn_last_usage = {}
        try:
            result = await self._request(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
                timeout=timeout,
            )
            return TurnResult(
                stop_reason=result.get("stopReason", "unknown"),
                text="".join(self._turn_text_chunks).strip(),
                thoughts="".join(self._turn_thought_chunks).strip(),
                tool_calls=self._turn_tool_calls,
                usage=dict(self._turn_last_usage),
            )
        finally:
            self._turn_session_id = None

    # ── 内部：发送 ───────────────────────────────────────────────────────
    async def _request(self, method: str, params: dict, timeout: float) -> dict:
        if not self.is_alive():
            raise ConnectionError("hermes acp process not alive")
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        await self._send_raw(msg)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out after {timeout}s")

    async def _send_raw(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    # ── 内部：接收循环 ───────────────────────────────────────────────────
    async def _reader_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    log.info("hermes acp stdout EOF")
                    break
                s = line.decode("utf-8", errors="replace").strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception as e:
                    log.warning("non-JSON frame from hermes acp: %r (err: %s)", s[:200], e)
                    continue
                await self._dispatch(obj)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("acp reader loop crashed")
        finally:
            # 进程意外退出：所有 pending 失败
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("hermes acp stdout closed"))
            self._pending.clear()

    async def _dispatch(self, obj: dict) -> None:
        # response 帧（有 id，没 method）
        if "result" in obj or "error" in obj:
            rid = obj.get("id")
            fut = self._pending.pop(rid, None) if rid is not None else None
            if fut is None:
                log.warning("response for unknown id=%s: %r", rid, obj)
                return
            if not fut.done():
                if "error" in obj:
                    err = obj["error"]
                    fut.set_exception(RuntimeError(
                        f"acp error: code={err.get('code')} message={err.get('message')}"
                    ))
                else:
                    fut.set_result(obj.get("result") or {})
            return

        # notification / request（有 method）
        method = obj.get("method")
        if method is None:
            log.warning("unknown frame: %r", obj)
            return

        if method == "session/update":
            self._on_session_update(obj.get("params") or {})
            return

        if method == "session/request_permission":
            await self._on_request_permission(obj)
            return

        # 其它 client capability：未实现就 method_not_found
        if "id" in obj:
            await self._send_raw({
                "jsonrpc": "2.0", "id": obj["id"],
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })
        else:
            log.debug("unhandled notification: method=%s", method)

    # ── session/update 聚合 ──────────────────────────────────────────────
    def _on_session_update(self, params: dict) -> None:
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")

        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                self._turn_text_chunks.append(content.get("text", ""))
        elif kind == "agent_thought_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                self._turn_thought_chunks.append(content.get("text", ""))
        elif kind == "tool_call":
            self._turn_tool_calls += 1
            title = update.get("title") or update.get("kind") or ""
            log.info("tool_call: %s", title[:120])
        elif kind == "tool_call_update":
            # 暂不展开内容，只做计数（已经在 tool_call 时记过）
            pass
        elif kind == "usage_update":
            self._turn_last_usage = {
                k: v for k, v in update.items() if k != "sessionUpdate"
            }
        elif kind == "available_commands_update":
            # 信息性事件，只在 init 后会出现一次，不参与 turn 聚合
            pass
        else:
            log.debug("unhandled session/update kind=%s", kind)

    async def _on_request_permission(self, obj: dict) -> None:
        """工具审批：一律选第一个 allow_* 选项，IM 用户没法点对话框。"""
        params = obj.get("params") or {}
        options = params.get("options") or []
        chosen = None
        for o in options:
            oid = (o.get("optionId") or "").lower()
            if "allow" in oid:
                chosen = o.get("optionId")
                break
        if chosen is None and options:
            chosen = options[0].get("optionId")
        log.info("auto-allow permission: %s", chosen)
        await self._send_raw({
            "jsonrpc": "2.0", "id": obj.get("id"),
            "result": {"outcome": {"outcome": "selected", "optionId": chosen}},
        })
