#!/usr/bin/env node
// Channels spike: 最小双向通道 server（docs/CHANNELS预研.md）
// POST /       → notifications/claude/channel 推进会话
// reply 工具   → 追加写 replies.log（真实实现会 POST 回 web_server）
// GET  /health → ok（探活）
import { createServer } from 'node:http'
import { appendFileSync } from 'node:fs'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { ListToolsRequestSchema, CallToolRequestSchema } from '@modelcontextprotocol/sdk/types.js'

const PORT = 8791
const REPLIES = new URL('./replies.log', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')

const mcp = new Server(
  { name: 'wxchan', version: '0.0.1' },
  {
    capabilities: {
      experimental: { 'claude/channel': {} },
      tools: {},
    },
    instructions:
      'Messages arrive as <channel source="wxchan" chat_id="...">. ' +
      'Reply with the reply tool, passing the chat_id from the tag.',
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: 'reply',
    description: 'Send a message back over this channel',
    inputSchema: {
      type: 'object',
      properties: {
        chat_id: { type: 'string', description: 'The conversation to reply in' },
        text: { type: 'string', description: 'The message to send' },
      },
      required: ['chat_id', 'text'],
    },
  }],
}))

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  if (req.params.name === 'reply') {
    const { chat_id, text } = req.params.arguments
    appendFileSync(REPLIES, JSON.stringify({ ts: new Date().toISOString(), chat_id, text }) + '\n', 'utf8')
    return { content: [{ type: 'text', text: 'sent' }] }
  }
  throw new Error(`unknown tool: ${req.params.name}`)
})

await mcp.connect(new StdioServerTransport())

let nextId = 1
createServer((req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    res.end('ok'); return
  }
  let body = ''
  req.on('data', c => { body += c })
  req.on('end', async () => {
    try {
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: { content: body, meta: { chat_id: String(nextId++) } },
      })
      res.end('ok')
    } catch (e) {
      res.statusCode = 500
      res.end(String(e))
    }
  })
}).listen(PORT, '127.0.0.1')
