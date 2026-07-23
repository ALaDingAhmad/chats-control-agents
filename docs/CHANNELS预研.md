# Claude Code Channels 预研

> ✅ 本预研的结论已于 2026-07-23 完全落地：`claude_channel` 成为默认且唯一的
> claude 后端，`claude_code` + mcp_bridge/cca-msg + chats-loop skill/hook +
> wait_for_message 循环 + marker 心跳租约（原文说"可删掉"的那些）**已全部删除**。
> 本文作为决策来源保留。
>
> 目标：验证用 Claude Code 官方 channels 机制（研究预览）替代 chats-loop
> 轮询架构——MCP server 主动把微信消息推进会话，Claude 用 reply 工具回复。
> 结论若成立，可删掉 wait_for_message 循环、marker 心跳租约、ESC 重唤等
> 整类"pull 模型补丁"。
> 状态：核心闭环已验证通过（2026-07-20）——方案成立，待设计 backend。

## 官方契约摘要（来源：code.claude.com/docs/en/channels-reference，2026-07-17 抓取）

### 通道 = 一个声明了特殊 capability 的 MCP server

- Claude Code 以 stdio 子进程方式拉起（和现在的 mcp_bridge 一样）。
- 硬性要求只有 `@modelcontextprotocol/sdk`（TS/JS）+ Node 兼容运行时
  （Node/Bun/Deno 均可）。**注意：官方路径是 JS SDK，Python mcp SDK 能否
  发自定义 method 的 notification 未验证——spike 先走 JS。**

### 三个协议要素

1. **声明 capability**（Server 构造器）：
   ```js
   capabilities: {
     experimental: {
       'claude/channel': {},            // 必须：注册为通道
       'claude/channel/permission': {}, // 可选：接收权限中继
     },
     tools: {},                         // 双向通道：暴露 reply 工具
   },
   instructions: '...'                  // 进 Claude system prompt，教它怎么处理事件/怎么回
   ```
2. **推消息**：`mcp.notification({ method: 'notifications/claude/channel',
   params: { content, meta } })`。`content` 变成 `<channel source="名字" ...>` 标签体，
   `meta` 的每个 key 变成标签属性（key 只能字母数字下划线，含连字符的会被静默丢弃）。
3. **回消息**：标准 MCP tool（如 `reply(chat_id, text)`），Claude 调它发回。

### 关键行为语义

- **忙时排队**：Claude 处理中收到的多条 notification 会攒着，下一个 turn
  一起投递，Claude 作为一组处理。——这直接替代我们的 inbox 追加排队逻辑。
- **无回执**：notification 的 await 只保证写进 transport，不保证 Claude 处理。
  会话没把 server 当 channel 加载、或 org policy 拦截时**静默丢弃**。
  需要投递确认就自己在 server 里记状态 + 用 reply 工具回报。
- **权限中继**：Claude Code 发 `notifications/claude/channel/permission_request`
  （request_id 五个小写字母、tool_name、description、input_preview）→ server
  转发到聊天端 → 用户回 `yes <id>` / `no <id>` → server 发
  `notifications/claude/channel/permission`（request_id + behavior: allow/deny）。
  本地终端弹窗同时保持打开，先到先用。**只有 sender 校验过的通道才许声明此能力。**
- **安全**：无门禁的通道 = 提示注入入口。必须按 **sender id**（不是群/房间 id）
  做 allowlist，不在名单的静默丢弃。

### 启用方式与限制

- 研究预览期自建通道不在官方 allowlist，必须
  `claude --dangerously-load-development-channels server:<mcp名>`（bare
  .mcp.json server）或 `plugin:<名>@<市场>`（插件形态）。启动时有全屏警告
  对话框需确认。
- 需要 claude.ai 登录或 Console key；Pro/Max 个人号无 org 检查直接可用。
- `-p` 非交互模式下支持 channels（需要终端输入的工具被禁用防卡死）。
- 契约在预览期可能变。
- 本地 CLI 2.1.210 已实测认识 `--channels` 和
  `--dangerously-load-development-channels` 两个 flag。

## 对 agent-bridge 的架构映射（结论成立时）

```
微信 ⇄ web_server（weixin 协议、路由、多会话——不变）
              ⇄ HTTP ⇄ channel server（新，JS，替代 mcp_bridge 的收发轮询）
                            ⇄ stdio notification/tool ⇄ claude 会话
```

- channel server 收 web_server 的 POST → notification 推进会话；Claude 调
  `reply` → channel server POST 回 web_server → outbox → weixin。
- **可服务性判定**：channel server 进程与 claude 同生命周期（stdio 子进程），
  "claude 进程活 = 通道活"。marker/心跳租约整套退役（bridge-owned 形态废弃）。
- **权限确认**：用 permission relay 替代 PTY 菜单 heuristic——微信里直接回
  `yes abcde`。
- daemon 仍负责拉起/看护 claude 进程（改用 `--dangerously-load-development-channels`
  启动，不再注入 /chats-loop trigger、不再解析 TUI）。

## Spike 验证项

| # | 验证点 | 结果 |
|---|---|---|
| 1 | JS channel server 被 claude 加载（dev flag + mcp-config） | ✅ TUI 无 strict 时通道注册成功 |
| 2 | 外部 POST → 空闲会话被唤醒并响应 | ✅ **PASS**（TUI，无 --strict-mcp-config） |
| 3 | Claude 调 reply 工具把回复送回 server | ✅ **PASS**（replies.log 落盘「通道打通」） |
| 4 | Claude 忙时连发多条 → 下轮一起投递不丢 | 待测 |
| 5 | 非交互（-p / stream-json）下通道可用（daemon 托管形态的前提） | ❌ 但**无需**：daemon 本就用 PTY 拉交互式 claude |
| 6 | 权限中继 request→verdict 闭环 | 待测 |

> **结论（2026-07-20 11:09）：channels 方案对本项目成立。** daemon 用 winpty 拉
> **交互式** claude（现有 claude_code backend 的既有形态）+ channel server，
> 外部 POST 能唤醒空闲会话并让它调 reply。#2/#3 核心闭环通过。#5（-p 常驻）
> 不成立但**不影响方案**——托管形态本就是交互式 PTY，不是 headless。

### Spike 记录

**v3（`-p` stream-json，`spike_run.py`，2026-07-20 上午）**
- `init` 日志 `mcp_servers: [{name: wxchan, status: connected}]`——MCP server 连上了。
- 但会话发完初始轮后 `type:result, subtype:success, terminal_reason:completed` **直接退出**。
  `-p` 是"跑完一轮就结束"，根本没有常驻会话给通道推消息。POST 进了 HTTP 端口，
  但会话已亡 → 无 reply。证据：`claude-out.log` 4 行止于 RESULT success。
- 结论：**`-p`/headless 不是 channels 的常驻宿主**，#5 判否。

**v4（交互式 TUI + winpty，`spike_tui.py`，2026-07-20 11:05）**
- 复刻 claude_code daemon 的 PTY 托管形态：`winpty.PtyProcess.spawn(["claude",
  "--mcp-config", "./mcp-config.json", "--strict-mcp-config",
  "--dangerously-load-development-channels", "server:wxchan",
  "--dangerously-skip-permissions"])`（**无 -p**）。
- dev-channel 全屏警告对话框实测文案：`WARNING: Loading development channels ...
  ❯1. I am using this for local development / 2. Exit / Enter to confirm`。
  默认选项 1，PTY 里直接喂 `\r` 即确认（spike 已自动处理）。
- 会话真的活着且待命：喂初始消息后屏上出现 `●ready ✻Cooked for 1s`（不是回显误判）。
- **关键卡点**：TUI 首屏 claude 自报一行错——
  > `server:wxchan · no MCP server configured with that name`
  即 `--dangerously-load-development-channels server:wxchan` 想引用的 channel
  与 `--mcp-config` 加载的 `wxchan` server **没接上**。通道从未注册成功，
  POST 的 notification 被静默丢弃（正合"会话没把 server 当 channel 加载时静默丢弃"）。
- 90s 内 `replies.log` 无内容 → #2 判否，但**根因是通道未绑定，不是 TUI 收不到事件**。

### 卡点根因（已定论，2026-07-20 11:09）

**`--strict-mcp-config` 会屏蔽 dev channel 的注册。** 消融实验（`spike_tui.py`
支持 `SPIKE_STRICT=0` 环境变量切换）：

| 变体 | 结果 |
|---|---|
| `--mcp-config … --strict-mcp-config --dangerously-load-development-channels server:wxchan` | ❌ TUI 报 `server:wxchan · no MCP server configured with that name`，通道未绑定 |
| `--mcp-config … --dangerously-load-development-channels server:wxchan`（**去掉 strict**） | ✅ 通道绑定，POST 唤醒会话，reply 落盘 |

**所以真正的托管启动命令里绝不能带 `--strict-mcp-config`。** 这条很反直觉——
strict 本意只是"忽略 .mcp.json 里的其它 server、只认命令行给的"，却连带把
dev channel 的隐式注册也砍了。现有 claude_code daemon 启动 claude 时**没带**
strict，所以迁移时保持这一点即可，但新 claude_channel backend 写启动命令时
要**显式注意别加回来**。

> 至于 v3（-p）为何 `init` 日志显示 `connected`：`-p` 下 MCP server 确实连上了
> （connected 指 MCP 层握手成功），但会话跑完一轮就退出，channel 事件无处投递；
> 且 -p 路径可能不触发同一条 strict-vs-channel 冲突。无论如何 -p 不是宿主，
> 此分支已废弃，不再深究。

## 开放问题（剩余，方案已成立后）

- **[已解决]** ~~`no MCP server configured with that name` 根因~~ → `--strict-mcp-config`
  屏蔽 dev channel 注册，去掉即通。见上「卡点根因」。
- **[已解决]** ~~dev flag 全屏警告在 PTY 下如何表现~~ → winpty PTY 下**会**弹，
  喂 `\r` 自动确认选项 1。
- **[待验，中]** #4 忙时排队：Claude 处理中连发多条 notification 是否下轮一起
  投递不丢。落 backend 前值得补一个 spike。
- **[待验，中]** #6 权限中继：permission_request → 微信回 `yes <id>` → verdict
  闭环。若用 `--dangerously-skip-permissions` 托管则本项可暂时搁置。
- **[设计,高]** dev-channel 警告对话框每次启动都弹——daemon 托管时 PTY 要在
  spawn 后自动喂 `\r` 确认（spike_tui.py 阶段1 已有可复用逻辑）。这是 backend
  启动序列的必要一环，不是可选项。
- 一个 claude 会话一个通道进程：多会话 = 多 claude 实例，与现有 per-alias
  daemon 模型一致，但要确认资源开销可接受。
- 预览期契约变更风险：channel server 要薄，协议细节集中在一处；且 dev flag 与
  strict 的这类隐式冲突随版本可能变，backend 要固化"实测通过的 flag 组合"并注释缘由。
