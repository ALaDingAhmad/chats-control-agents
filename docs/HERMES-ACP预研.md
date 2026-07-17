# Hermes ACP Spike 结论

> 2026-06-10 完成。研究 hermes-agent 作为 agent-bridge 第二个 backend 的可行性。
> 结论：**ACP 路走通，可以动 backend**。

## TL;DR

- **选 ACP，不选 tui_gateway**。tui_gateway 是 hermes 自家 Node TUI 的私有 JSON-RPC（方法集 `prompt.submit / terminal.resize / clipboard.paste / image.attach …`），跟 hermes 版本绑死，没有 SDK。ACP 是公开协议（`agent-client-protocol` SDK），跨 agent 标准。
- 协议、握手、多 session 隔离、cwd 路由、工具调用、文本流、图片入参能力 — **全部实测走通**（见 `scripts/spike_acp_raw.py` 的输出）。
- ACP 协议**支持**单进程多 session，但 **agent-bridge 落地不用这个**（详见 `后端设计.md`）：我们仍然走 1 alias = 1 daemon = 1 hermes acp 子进程，保持跟 claude_code backend 同构。代价是不能共享 hermes 启动开销，好处是失败域隔离、零改 router/spawn 抽象。

## 关键架构事实（落 backend 前必读）

### 进程模型：1 alias = 1 daemon = 1 hermes acp 子进程

```
agent-bridge web/weixin runtime
  └─ (per alias) daemon  ← hermes_acp/daemon.py
        ├─ poll inbox.txt
        ├─ stdio JSON-RPC ↔ subprocess: hermes acp
        │                                 └─ 一个 ACP session（hermes 内部）
        └─ write outbox.txt
```

- daemon 在消息路径上（"路径内"型 backend），跟 claude_code 的"路径外"形态相反。
  详见 [`后端设计.md`](后端设计.md)。
- 跟 claude_code 同构的部分：1 alias = 1 daemon、`meta.json.daemon_pid` 判活、
  `spawned_pids.jsonl` 记 child PID（这里是 `hermes acp` 子进程）。
- 不一样的部分：daemon 进程本身参与消息流转 —— inbox poll + ACP 收发 + outbox 写。
  daemon 死了消息立刻断（不像 claude_code 那样 mcp_bridge 还能撑一会）。
- ACP 协议本身**支持** N 个 session 共享一个 `hermes acp` 子进程，但落地不用 ——
  现有 router/spawn/meta 一套都按 1 alias = 1 daemon 建模，且单机 1-2 alias 时
  hermes 启动开销不是瓶颈。详细理由见 BACKEND-DESIGN 末尾的"历史决策"。

### ACP 协议要点

- **stdio JSON-RPC**，newline-delimited（不是 LSP Content-Length framing）。
- 字段名走 **camelCase by alias**：`mcpServers / sessionId / sessionUpdate / agentCapabilities`，不是 snake_case。
- 初始化：`initialize` → `session/new(cwd, mcpServers=[])` → `session/prompt(sessionId, prompt=[…blocks…])`。
- 流式输出：hermes 通过 `session/update` notification 推 `agent_message_chunk` / `agent_thought_chunk` / `tool_call` / `tool_call_update` / `usage_update` 等事件。客户端必须聚合 `agent_message_chunk.content.text` 拼出完整回复。
- 工具审批：hermes 发 `session/request_permission` 请求，客户端必须回 `{outcome: {outcome: "selected", optionId: "allow_…"}}`。
- 图片输入：`prompt=[ImageContentBlock(data=b64, mime_type="image/jpeg"), TextContentBlock(...)]`。hermes adapter 把它转成 OpenAI-style `image_url` 喂下游 vision 模型（`acp_adapter/server.py:378`）。

### 关键事件类型（实测捕获）

```
available_commands_update : 1  (会话初始化时一次)
usage_update              : N  (token 计数)
agent_thought_chunk       : N  (思考流，可选展示)
tool_call                 : N  (工具开始执行)
tool_call_update          : N  (工具完成 / 进度)
agent_message_chunk       : N  (最终回复的流式 chunk，合并即文本)
session/request_permission: N  (工具审批)
```

落到 agent-bridge 的 outbox.txt 模型时：
- 只把 `agent_message_chunk` 累积成"最终回复"。
- 思考流 / 工具调用进度先**不**进 outbox（outbox 是"最新一条待推送的回复"，不是流）。
- 工具审批一律 auto-allow（用户在 IM 里没法点对话框）。

## SDK 兼容性坑

- 官方 `agent-client-protocol` SDK 在我们环境跑下来：`session_update` 回调**没触发**（hermes 真的发了，raw stdio 抓得到，但 SDK 的 dispatcher 静默吞了）。
- 还没定位 SDK 的具体根因，但**结论**是 backend 实现**不依赖 SDK 的 Client 抽象**，直接用 raw JSON-RPC（参考 `scripts/spike_acp_raw.py` 的 RawClient）写。
- 好处：少一层依赖；JSON-RPC 自己写也就 150 行；schema 用 dict 不用 pydantic，跟 hermes 各版本兼容更稳。

## 已踩 / 待避坑

- **hermes 启动慢**：第一次 `session/new` 因为要初始化 LexAI client、probe context length、做 vision auto-detect，**实测要 60-80 秒**。后续 session/new 就快了（agent 实例缓存）。`ensure_session` 的等待要 >= 120s，不能用 30s 默认。
- **LexAI 是 reasoning 模型**：`reasoning_content` 字段 hermes 已正确转成 `agent_thought_chunk`，不影响 `agent_message_chunk` 走最终回复。但**思考流非常长**（pwd 任务都能产生 30+ 条 thought chunk），要确保不把它误推到 outbox。
- **hermes config**：用户 `~/.hermes/config.yaml` 的 `model.default` + `custom_providers[].model` 决定模型 ID。**改这两行就改了用户日常 hermes CLI 的行为**——backend 不应该改它，要靠 hermes 启动时的环境变量或 CLI 参数。如果非要切模型，走 hermes 的 `/model` slash command（hermes 的 `available_commands_update` 自动发了支持列表）。
- **模型名漂移**：LexAI 端点的模型 ID 会改（`LexAI/LexAI-Agent-Svc` → `LexAI/LexAI-Svc`）。backend 启动时先 `/v1/models` 探针，确认配的模型还在；不在就抛错让用户改 config。**spike 当天（2026-06-10）端点上只剩 `LexAI/LexAI-Svc`，我已经把 `~/.hermes/config.yaml` 的 `model.default` 和 `custom_providers[0].model` 都改成了新名**（备份在 `~/.hermes/config.yaml.bak.spike.20260610_130443`）。如果端点恢复 `-Agent-Svc`，记得改回去；不改的话 hermes CLI 日常使用不受影响（它已经在用新名了）。

## 跟 claude_code backend 对比

| 维度 | claude_code | hermes_acp |
|---|---|---|
| 进程拓扑 | 1 alias = 4 进程 | N alias 共享 1 子进程 |
| 启动时间 | ~10s (TUI 加载 + 触发 chats-loop) | 首次 ~80s，后续 ~1s |
| 输出捕获 | PTY 字节流 + ANSI 剥离 | 结构化 JSON-RPC 事件 |
| 工具控制 | child claude 自决 | hermes 自决（自带 terminal/code/web tools） |
| 多模态输入 | 不支持（PTY 没法粘图） | 原生 ImageContentBlock |
| 限额自恢复 | daemon 监控 PTY + auto press 3 | 没做，待补（hermes 自己有 usage_update 可观察） |
| 跨 session 隔离 | 进程级隔离（最强） | sessionId 隔离（hermes 内部状态） |

## 下一步

执行计划（按顺序）：

1. **抽 `core/daemon_lifecycle.py`**（~80 行）：把 claude_code/daemon.py 里
   通用部分（CLI 解析 / meta 写盘 / spawned_pids.jsonl / atexit / SIGINT）
   提出来。详细职责分类见 后端设计.md 的"daemon 职责拆分"段。
2. **改 `backends/claude_code/daemon.py`**：调用 daemon_lifecycle 那套，
   保留 winpty / TUI ready / chats-loop 触发 / rate-limit 看门狗 这些
   专属逻辑。**行为零变化**（回归 baseline）。
3. **新建 `backends/hermes_acp/daemon.py`**：
   - 用 daemon_lifecycle 起骨架
   - subprocess + stdio JSON-RPC（蓝本 `scripts/spike_acp_raw.py`）
   - `initialize` → `session/new(cwd)` → 进入主循环
   - 主循环：poll inbox.txt → `session/prompt` → 收 `session/update` 事件
     → 聚合 `agent_message_chunk.content.text` → 写 outbox.txt
   - `session/request_permission` 一律 auto-allow（IM 用户没法点对话框）
   - 思考流 / 工具调用进度先**不**进 outbox，避免覆写最终回复
4. **改 `core/spawn.py` 1 处**：按 `meta.backend` 字段选 daemon 模块（缺省
   `claude_code` 向后兼容）。
5. **手动测试**：手工创建一个 `chat_sessions/hermes-test/meta.json` 写
   `backend: "hermes_acp"`，触发 spawn，验证 router → inbox → daemon → ACP →
   outbox → web 全链路。

不属于本期：

- 默认 backend 切换策略（用户体验问题，先让两个 backend 共存跑一周）
- 图片入参从 channel 端怎么塞（要扩 `Backend.send` 签名 / channel 端先解出
  二进制；先把文本路打通）
- hermes 进程崩了的 graceful degradation（先看实际崩多频再设计）

## 参考文件

- spike 脚本：`scripts/spike_hermes_acp.py`（SDK 路，证明 SDK 兼容有坑）
- spike 脚本：`scripts/spike_acp_raw.py`（raw JSON-RPC，是 backend 的实现蓝本）
- hermes 源码：`F:/wslshare/hermes-agent/acp_adapter/server.py`（重点：`initialize`, `new_session`, prompt 处理, ImageContentBlock 转 OpenAI parts）
- ACP SDK 方法名映射：`acp.meta.AGENT_METHODS`
