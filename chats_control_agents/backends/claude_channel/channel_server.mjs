#!/usr/bin/env node
// claude_channel backend —— 纯协议转换器（不碰文件系统）。
//
// 契约见 docs/后端设计.md「claude_channel backend 契约」。分工：
//   - daemon.py（Python）管所有文件 IO（poll inbox.txt / 写 outbox.txt）。
//   - 本进程（channel_server）只做协议转换，作为 claude 的 stdio 子进程运行：
//       * HTTP  POST /inject  ← daemon 把 inbox 新消息推来 → notifications/claude/channel 推进会话
//       * MCP   reply 工具     ← claude 调它回消息 → POST 回 daemon 的 /reply 回调口
//       * HTTP  GET  /health  → ok（daemon 探活）
//
// 端口通过环境变量传入（daemon spawn claude 时设，child claude 继承，再传给本进程）：
//   CHANNEL_INJECT_PORT   本进程 HTTP 监听端口（收 daemon 的 /inject、/health）
//   CHANNEL_REPLY_URL     daemon 的 reply 回调 URL（如 http://127.0.0.1:8792/reply）
// 缺省仅用于脱离 daemon 裸测；生产必由 daemon 显式设置。
import { createServer } from 'node:http'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'

const INJECT_PORT = Number(process.env.CHANNEL_INJECT_PORT || 8791)
const REPLY_URL = process.env.CHANNEL_REPLY_URL || 'http://127.0.0.1:8792/reply'
const CHANNEL_NAME = process.env.CHANNEL_NAME || 'wxchan'

// stderr 日志（stdout 被 MCP stdio transport 占用，绝不能往 stdout 写非协议内容）
const logerr = (...a) => process.stderr.write(`[channel_server] ${a.join(' ')}\n`)

const mcp = new Server(
  { name: CHANNEL_NAME, version: '1.0.0' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} },
      tools: {},
    },
    instructions:
      `Messages arrive as <channel source="${CHANNEL_NAME}" chat_id="...">. ` +
      'Each is a user message relayed from a remote IM channel (e.g. WeChat) — ' +
      'the user is NOT at this terminal and CANNOT see anything you print here. ' +
      'The ONLY way to answer them is to call the `reply` tool with their chat_id ' +
      '(from the tag) and your message text. ' +
      'You MUST call `reply` for every channel message — text you print in the ' +
      'session is invisible to them and is lost. Always finish by calling `reply`. ' +
      'Keep replies concise for a phone screen.',
  },
)

// reply 工具：claude 调它 → POST 回 daemon，daemon 写 outbox.txt。
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: 'reply',
    description: 'Send a message back over this channel to the IM user.',
    inputSchema: {
      type: 'object',
      properties: {
        chat_id: { type: 'string', description: 'The conversation to reply in (from the channel tag)' },
        text: { type: 'string', description: 'The message text to send' },
      },
      required: ['chat_id', 'text'],
    },
  }],
}))

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  if (req.params.name !== 'reply') {
    throw new Error(`unknown tool: ${req.params.name}`)
  }
  const { chat_id, text } = req.params.arguments || {}
  try {
    const resp = await fetch(REPLY_URL, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ chat_id: String(chat_id ?? ''), text: String(text ?? '') }),
    })
    if (!resp.ok) {
      logerr(`reply callback HTTP ${resp.status}`)
      return { content: [{ type: 'text', text: `delivery failed (HTTP ${resp.status})` }], isError: true }
    }
    return { content: [{ type: 'text', text: 'sent' }] }
  } catch (e) {
    logerr(`reply callback error: ${e}`)
    return { content: [{ type: 'text', text: `delivery error: ${e}` }], isError: true }
  }
})

await mcp.connect(new StdioServerTransport())
logerr(`MCP connected, channel="${CHANNEL_NAME}"`)

// HTTP 面：收 daemon 的 /inject（把 inbox 消息推进会话）+ /health。
let nextChatId = 1
createServer((req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    res.end('ok')
    return
  }
  if (req.method === 'POST' && req.url === '/inject') {
    let body = ''
    req.on('data', c => { body += c })
    req.on('end', async () => {
      // body 约定：{ text: "...", chat_id?: "..." }；兼容纯文本 body。
      let text = body, chatId = null
      try {
        const parsed = JSON.parse(body)
        if (parsed && typeof parsed === 'object') {
          text = parsed.text ?? ''
          chatId = parsed.chat_id ?? null
        }
      } catch { /* 非 JSON：整体当文本 */ }
      if (chatId == null) chatId = String(nextChatId++)
      try {
        await mcp.notification({
          method: 'notifications/claude/channel',
          params: { content: String(text), meta: { chat_id: String(chatId) } },
        })
        res.end('ok')
      } catch (e) {
        logerr(`inject notification failed: ${e}`)
        res.statusCode = 500
        res.end(String(e))
      }
    })
    return
  }
  res.statusCode = 404
  res.end('not found')
}).listen(INJECT_PORT, '127.0.0.1', () => {
  logerr(`HTTP listening on 127.0.0.1:${INJECT_PORT} (inject/health)`)
})
