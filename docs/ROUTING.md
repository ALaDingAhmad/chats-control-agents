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

2. **passthrough (`//xxx`)** — `commands.strip_passthrough_prefix` 去掉一层
   `/`，让 agent 看到的是 `/xxx`，剩下的文本走普通消息路径。

3. **没有当前会话** — `sx.get_current()` 返 None。回提示让用户去 dashboard
   建一个；当前这条消息丢弃。

4. **idle gate** — `sx._last_active(alias)`（即 inbox/outbox/history 三个
   文件 mtime 的最大值）超过 `IDLE_THRESHOLD_SECS` 就触发，返回 `/proj`
   列表让用户显式选继续或新开。**不动 daemon**，当前消息丢弃。阈值在
   `core.router.IDLE_THRESHOLD_SECS`（目前 2 小时）。

5. **daemon 死了就拉起** — 调 `spawn.ensure_daemon_alive(alias)`。返 False
   就把"agent 拉起失败"作为 reply 抛回去。

6. **交给后端** — 把 `text` 写进 `inbox_path(alias)`，追加一条 history，返
   `routed=True`。

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
