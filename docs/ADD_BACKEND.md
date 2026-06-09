# 加新后端

后端是"把用户消息变成回复"的那一端：Claude Code TUI（当前）、OpenClaw、
Hermes、直接 Anthropic API、本地 LLM……

## 骨架

```
chats_control_agents/backends/<name>/
├── __init__.py
├── adapter.py     （可选）实现 backends.base.Backend
└── …              后端特有的文件（daemon / RPC client / …）
```

## 两种后端形态

### 进程型（如 claude_code）

- **每个会话一个 daemon 进程**（per alias）。daemon 在 PTY 里 spawn 真正的
  agent（如 `claude.exe`），保活、drain 输出。
- agent 通过加载进自己宿主进程里的 **MCP 工具**和 bridge 通信。工具读写
  `chat_sessions/<alias>/inbox.txt` 和 `outbox.txt`。
- bridge ↔ 后端的边界就是文件。松耦合，任一边重启都能扛。

优点：
- agent 拿到完整 TUI / 交互环境。已有工具（文件操作、shell、MCP 服务器）
  透明可用。
- bridge 不需要了解 agent 的协议。

缺点：
- 每个会话开销大：完整 TUI 进程、MCP 服务器等。
- spawn 慢（claude_code 大概 5-10 秒）。
- agent 状态可能漂移（如 rate-limit 弹窗挡住 TUI —— 看
  `daemon.py` 的 rate-limit 看门狗）。

### API 型（假想：anthropic_api）

- 无状态 HTTP 后端。没有 daemon，没有 session 绑定的进程。每条入站消息
  转成一次 API 调用，带上会话历史。
- bridge 在 `history.json` 里存对话状态，每轮 replay 给 API。

优点：
- 没有 spawn 成本，没有进程管理。
- 容易扩，容易推理。

缺点：
- bridge 得显式管 tool calls / 循环 / history 截断。
- 没交互 TUI；用户没法通过 agent 跑 shell 命令，除非 bridge 这边再加胶水。

## 必须做的事

1. **拉起 / 连接**：`ensure_session(alias, cwd)` —— 保证这个 alias 有东西能
   回话。进程型：daemon 死了就 spawn 一个。API 型：no-op。
2. **接消息**：读 `inbox_path(alias)`。进程型：agent 进程里的 MCP 工具读。
   API 型：后端 adapter 里的 worker 读。
3. **回复**：写 `outbox_path(alias)`，格式 `"[HH:MM:SS]\n<reply>\n"`，让 web
   `/poll` 和微信 outbox watcher 能识别。
4. **跟踪存活**：写 `meta.json` 含 `daemon_pid` / `child_pid`，让
   `sessions.list_sessions()` 能报告 online 状态。
5. **记录 spawn**：追加到 `spawned_pids.jsonl`，让清理工具能区分后端 spawn
   的进程和用户手开的（之前撞过孤儿 claude.exe 群杀的坑）。

## 参考实现：claude_code

`backends/claude_code/daemon.py`：

- 解析 CLI：`python -m chats_control_agents.backends.claude_code.daemon <alias> [<cwd>]`
- 在 `winpty` PTY 里 spawn `claude.exe --dangerously-skip-permissions`
- 等 TUI ready（看欢迎屏标记，**ANSI-blind**——见 `docs/DAEMON-LIFECYCLE.md`）
- 自动应答 trust-folder 对话框，然后发 `/chats-loop` 触发 skill
- 写 `meta.json`、追加到 `spawned_pids.jsonl`
- drain 循环带看门狗：PTY 出现 `You've hit your limit` 或
  `/rate-limit-options` 就按 3+Enter 关掉，通过 `outbox.txt` 发用户可见
  通知，每 5 分钟重试 trigger
- `atexit` 清理：杀 child、标 meta offline

完整生命周期 + cold-start 细节在 [`docs/DAEMON-LIFECYCLE.md`](DAEMON-LIFECYCLE.md)。

`backends/claude_code/mcp_bridge.py` 是 child claude 加载的 MCP 服务器，
提供两个工具：

- `wait_for_message(timeout_seconds=0)` —— 500ms 间隔轮询
  `inbox_path(ALIAS)`，有消息就返回。空轮询用指数退避（300s → 600s → …）
  作为 `TIMEOUT (waited Xs, next will be Ys)` 透传给 LLM。
- `send_chat_response(reply)` —— 写 `outbox_path(ALIAS)` 后返回。

`CHATS_LOOP_ALIAS` 环境变量告诉 mcp_bridge 它服务哪个会话；daemon spawn
child claude 时设这个 env。

## Backend ABC

`backends/base.py` 定义了 `Backend`，含 `ensure_session / send /
is_session_alive / end_session / session_status`。现有 claude_code 代码
**还没继承**——ABC 是文档化契约，不强制。

## 常见坑

- **PID 复用**：永远别只信 PID。`pid_track.list_daemon_child_pids()` 会用
  `psutil.Process(pid).create_time()` 跟 spawn 时记录的值交叉验证。别绕过。
- **别杀用户手开的 agent 进程**：所有清理脚本必须用
  `pid_track.list_daemon_descendants()` 限定杀伤范围。用户自己的交互
  `claude.exe` 永远不在 `spawned_pids.jsonl` 里，所以这套行得通。
- **rate limit 是真的、且在进程内无解**：上游 AI 提供方限流时，agent 进程
  经常弹出需要键盘交互才能关掉的 modal。daemon 必须能识别并自动按键，
  否则会话锁死。
