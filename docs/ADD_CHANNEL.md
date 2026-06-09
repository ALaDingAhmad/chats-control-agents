# 加新渠道

渠道是"真人在这里打字的入口"：微信、浏览器、未来的飞书 / Slack / Telegram /
邮箱……

## 骨架

```
chats_control_agents/channels/<name>/
├── __init__.py
├── protocol.py    HTTP / WebSocket / SDK 调用（IM 用啥就用啥）
├── state.py       持久凭证、per-peer 状态（gitignored）
└── adapter.py     （可选）实现 channels.base.Channel
```

状态文件放项目根 `<channel>_state/`，加进 .gitignore。

## 必须做的事

1. **收消息**：long-poll、websocket、webhook —— IM 给啥就用啥。把原始 payload
   翻译成 `(peer_id, text, context_dict)`。
2. **走 core.router 分发**（**不要**直接调 `commands` / `sessions` /
   `spawn`——那是核心层的事）：
   ```python
   from chats_control_agents.core import router

   outcome = await router.route_inbound(text, source=f"<channel>:{peer_id[:8]}")

   if outcome.reply is not None:
       send_to_peer(peer_id, outcome.reply)
       # 涵盖：slash 命令回复、idle gate 的 /proj、no-session 提示、spawn 失败
   elif outcome.routed:
       if outcome.alias:
           # 记下这个 alias 当前归哪个 peer，给 outbox 反向投递用
           persist_alias_peer(outcome.alias, peer_id)
       show_typing_indicator(peer_id)            # 渠道可选 UX
   ```
   完整契约：[`docs/ROUTING.md`](ROUTING.md)。
3. **回消息**：watch 每个 session 的 `outbox_path(alias)`；有新内容就推给最近
   往这个 alias 写过的 peer。第 2 步落的 `alias_peer` 映射告诉你是谁。
4. **注册到 web/server.py**：
   - HTTP 路由（状态页、OAuth callback 等）→ `routes/<name>.py`
   - 长期任务（longpoll、watcher）→ `web/<name>_runtime.py`
   - 接进 `_lifespan` 启动钩子，让任务跟 web_server 一起起来。

## 参考实现：weixin

`channels/weixin/protocol.py` 暴露：

- `get_qrcode(session)` —— 拉首次登录二维码
- `poll_qrcode_status(session, qrcode, base_url)` —— 看扫码进度
- `get_updates(session, base_url, token, sync_buf)` —— long-poll 入站
- `send_text(session, base_url, token, to_user_id, text, context_token)` —— 出站
- `extract_text_and_meta(msg)` —— 把原始入站 msg dict 翻译成
  `(sender, text, context_token)`，非文本 payload 返回 None。

`channels/weixin/state.py` 持久化：

- `weixin_state/account.json` —— bot_token + base_url + ilink_bot_id
- `weixin_state/context_tokens.json` —— per-peer 回复线程 context
- `weixin_state/alias_peer.json` —— `{alias: peer_id}` 映射，让 outbox
  watcher 在 web_server 重启后还能把 Claude 的回复发到对的人

`web/weixin_runtime.py` 跑：

- `_inbound_longpoll(account)` —— 循环调 `get_updates`，把消息派给 router
- `_outbox_watcher(account)` —— 遍历每个会话的 outbox，新内容发给对应 peer
- `qr_login_loop()` —— QR 登录期间轮询扫码状态
- `bootstrap_weixin()` —— 启动钩子：有存好的账号就恢复 long-poll

新渠道照着这个形态做。

## Channel ABC

`channels/base.py` 定义了 `Channel` 抽象类，含 `start / stop /
is_connected / send / status` 和 `InboundMessage` 信封。现有 weixin 代码
**没继承**——ABC 是文档化契约，不强制。新渠道鼓励继承。

## 常见坑

- **PC 微信换行渲染**：iLink Bot 的 Windows 桌面客户端把多行纯文本压成一行。
  手机端正常。不要试图从 bridge 侧"修"，PC 微信就当成已知坏渲染对待。
- **连接抖动**：任何 long-poll 渠道都会偶尔"Server disconnected"。指数退避
  + 重试，不要冒泡到用户。
- **凭证失效**：把"连接掉了、可重试"和"凭证死了、得重新登录"区分开。微信
  ret -14 / -2 是后者——停 long-poll，提示用户重新扫码。
