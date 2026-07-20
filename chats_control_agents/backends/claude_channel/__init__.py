"""claude_channel backend —— Claude Code channels（推模型）。

替代 chats-loop 轮询：channel_server（JS，claude 进程内 stdio 子进程）通过 MCP
channel notification 主动把 inbox 消息推进会话，claude 用 reply 工具回复。

契约见 docs/后端设计.md「claude_channel backend 契约」+ docs/CHANNELS预研.md。

组成：
  daemon.py          —— Python：winpty 拉 claude、poll inbox、写 outbox、reply 回调
  channel_server.mjs —— JS：纯协议转换器（/inject → notification；reply → 回调）
  package.json       —— @modelcontextprotocol/sdk 依赖（需先 npm install）

启动死记：不带 --strict-mcp-config（会屏蔽 dev channel 注册）。
"""
