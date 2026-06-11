# 入站路由契约

任何渠道（今天是微信，未来可能是飞书/Slack/…）收到用户消息后，统一交给
`core.router` 决策，渠道自己不直接调 `commands` / `sessions` / `history` /
`spawn`——这些是核心层的事。

## 入口

```python
from chats_control_agents.core import router

outcome: router.RouteOutcome = await router.route_inbound(
    text="...",                       # 用户原文，渠道收到啥就传啥
    source="weixin:o9cq809U",         # 写进 history.json 的来源标签
)
```

`route_inbound` 是 `async`——内部可能 `await ensure_daemon_alive`。其余都是
同步文件 I/O。

## RouteOutcome 契约

```python
@dataclass
class RouteOutcome:
    reply:   Optional[str] = None   # 把这段文本回给用户，到此为止
    routed:  bool          = False  # 消息已写入会话 inbox
    alias:   Optional[str] = None   # 涉及哪个会话
```

`reply` 和 `routed` 互斥——只有一个会带真实信号。渠道按结果分流：

| 结果         | 渠道动作                                                              |
|--------------|----------------------------------------------------------------------|
| `reply` 非空 | 通过渠道的发送通道把文本发给用户；`alias` 不参与本次投递路由           |
| `routed=True`| 消息已交给后端；做渠道自己的 UI（如对方正在输入气泡）                  |
| 两者都空     | 不应该发生；真发生就记日志当静默失败处理                                |

`alias` 是信息字段。渠道可以拿它更新自己的 per-alias 簿记——比如微信会把
`alias → peer_id` 落到 `weixin_state/alias_peer.json`，让 outbox watcher
在 web_server 重启后还能把 Claude 的回复送到对的人。

## route_inbound 内部决策顺序

1. **是否 slash 命令** — `commands.is_command(text)` 对单 `/` 命令、或者
   `/proj` 选号窗口内的纯数字返回 True。命中就跑 `commands.handle_command`
   返回 reply，不动后端。

   命令里有一类"**元命令**"——不直接操作当前会话，而是改"之后建会话时的
   参数"。目前只有 `/backend [<name>]`：看/切默认 backend，落
   `chat_sessions/_default_backend.txt`，之后 `/proj N` / `/proj 0` 建会话
   时读它。详见 `BACKEND-DESIGN.md` "默认 backend 契约"段。

2. **passthrough (`//xxx`)** — `commands.strip_passthrough_prefix` 去掉一层
   `/`，让 agent 看到的是 `/xxx`，剩下的文本走普通消息路径。

3. **没有当前会话** — `sx.get_current()` 返 None。回提示让用户去 dashboard
   建一个；当前这条消息丢弃。

4. **idle gate** — `sx._last_active(alias)`（即 inbox/outbox/history 三个
   文件 mtime 的最大值）超过 `IDLE_THRESHOLD_SECS` 就触发，返回 `/proj`
   列表让用户显式选继续或新开。**不动 daemon**，当前消息丢弃。阈值在
   `core.router.IDLE_THRESHOLD_SECS`（目前 2 小时）。

5. **daemon 死了就拉起** — 调 `spawn.ensure_daemon_alive(alias)`。返 False
   就把"agent 拉起失败"作为 reply 抛回去。spawn 成功的话，**会顺带启动
   一个就绪通知后台任务**（见下节"就绪通知"）。

6. **交给后端** — 把 `text` 写进 `inbox_path(alias)`，追加一条 history，返
   `routed=True`。

## 就绪通知

任何由 bridge 主动拉起 daemon 的场景（router 的 ensure_daemon_alive、
autospawn worker 处理 `/proj N` 或 `0` 写进的队列项），spawn **成功**后必须
启动一个后台 watch 任务，监听这个 alias 的 chats-loop skill 何时真激活，
然后向用户广播就绪/失败。

### 信号源

mcp_bridge 在 child claude 第一次进入 `wait_for_message` 时 touch 一个
marker 文件：

```
~/.claude/.chats-loop-active-<alias>
```

这个文件存在 = skill 已激活 = 用户消息真的能被消费。文件不出现 = skill
没起来（trust 弹窗卡住、READY_MARKERS 不匹配、trigger 没进 TUI…）。

### 行为契约

`spawn.watch_ready(alias)` 是一个 `async` 函数，由调用 spawn 的位置
`asyncio.create_task` 起：

| 情况 | 动作 |
|---|---|
| `READY_NOTIFY_TIMEOUT` 内 marker 出现 | 写 outbox：`✅ 会话 <alias> 已就绪，发消息试试` |
| `READY_NOTIFY_TIMEOUT` 超时 marker 仍不存在 | 写 outbox：`⚠️ 会话 <alias> 拉起超时，可能 child claude 卡在了某个弹窗。看 chat_sessions/<alias>/pty.log` |
| 期间 daemon 进程死了 | 写 outbox：`⚠️ 会话 <alias> 启动后异常退出，看 daemon.log` |

`READY_NOTIFY_TIMEOUT` 在 `core.spawn` 里（默认 60 秒——含 trust 弹窗 +
TUI 渲染 + `/chats-loop` 触发 + skill 初始化 + 第一次 wait_for_message
的累计时间）。

### 为什么是写 outbox，而不是渠道单独发送

outbox 是后端给前端发消息的现有通道——所有渠道都已经接好 outbox_watcher。
通过 outbox 发就绪通知意味着：(1) 渠道一行代码不用动；(2) 用户在哪个渠道
聊就在哪个渠道收到，自动多渠道适配；(3) 同样的通知在 web UI `/poll`
也能拿到，无差别。

唯一注意：outbox 是"最新一条待推"的单槽，watch_ready 写完通知后如果
child claude 立刻又回了别的，可能盖掉就绪通知——所以**就绪通知必须在
child claude 收到第一条消息之前发出去**，靠 marker 文件的 touch 时机
天然保证。

### 不发就绪通知的场景

- 用户手动起的 daemon（`python -m chats_control_agents.backends.claude_code.daemon`）
  ——他自己知道在等，不需要桥通知。
- web dashboard 的"开始新会话"按钮——HTTP 请求本身会同步等 ready 后才返
  回，UI 已经能反映状态。

## 为什么不让渠道自己做

这套步骤放进核心，新加渠道就只剩一件事：解析协议帧 → 调 `route_inbound`
→ 按 outcome 分流。2 小时 idle gate、`/proj` 重发、按需拉 daemon、no-session
提示——只要新渠道接进来这套立刻可用。

## 不在 router 里的事

- 渠道协议（HTTP、long-poll、auth）—— 渠道自己负责。
- 渠道的持久化（`alias_peer.json`、peer 上下文 token）—— 渠道自己负责，因为
  peer 身份本就是渠道概念。
- 后端协议（`inbox.txt` 怎么进 child claude、`outbox.txt` 怎么生成）—— 后端
  自己负责。
- UI 装饰（输入气泡、已读回执、消息编辑）—— 渠道自己负责，在拿到
  `routed=True` 时触发。

## idle gate 设计动机

旧行为：任何入站都会静默拉起当前会话的 daemon。隔几天没说话，"新"消息
落到陈年 alias 里，用户也没法干净切换。idle gate 强制在 2h 节点显式让用户
选——紧凑对话不会触发，第二天回来才出现，是正好的位置。

阈值用秒不是分钟，方便以后扩（按渠道差异化、按时间段调）。
