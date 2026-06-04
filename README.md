# agent-bridge

A pluggable bridge between IM channels (WeChat, future Feishu / Slack / …)
and AI execution backends (Claude Code, future OpenClaw / Hermes / …).

Lets you chat with a local Claude Code session from your phone via WeChat.
Add a channel: now reachable from that IM too. Add a backend: now any
channel can talk to that AI.

## Quick start

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Configure workspace roots (where /proj scans)
#    Default ["D:/aiproject", "F:/wslshare"] — edit config.json if needed.

# 3. Start the web server
python -m agent_bridge.web.server
# → http://127.0.0.1:8765/

# 4. (Optional) Connect WeChat
#    Open http://127.0.0.1:8765/weixin → scan QR with your phone WeChat.

# 5. Spawn a Claude Code session for some project
#    Either via /proj in the chat UI (recommended), or manually:
python -m agent_bridge.backends.claude_code.daemon <alias> <cwd>
```

## In-chat commands

| command          | what                                           |
|------------------|------------------------------------------------|
| `/proj`          | list workspace projects (paged, 25/page)       |
| `/proj more`     | next page                                      |
| `<N>`            | (after /proj) pick project #N — switch / spawn |
| `/list`          | list all sessions and status                   |
| `/use <alias>`   | switch current session                         |
| `/new <alias> [<cwd>]` | create new session (prints daemon start cmd) |
| `/end <alias>`   | terminate session (60s confirm)                |
| `/rename <new>`  | rename current session (offline only)          |
| `/help`          | help                                           |
| `//xxx`          | pass `/xxx` through to the AI agent            |

## Layout

```
agent_bridge/
├── core/         shared logic: sessions, commands, projects, paths
├── channels/     IM adapters (weixin; future feishu, slack, …)
├── backends/     AI adapters (claude_code; future openclaw, hermes, …)
└── web/         Starlette HTTP layer
docs/
├── ARCHITECTURE.md
├── ADD_CHANNEL.md
└── ADD_BACKEND.md
scripts/
├── kill_daemon_children.py     safely kill all backend-spawned processes
└── restart_all.ps1             Windows: full restart
```

See `docs/ARCHITECTURE.md` for the data flow diagram and module map.

## Origin

Forked / restructured from
[`claude-mcp-bridge`](../claude-mcp-bridge/) (single-channel, single-backend).
History before split is in that sibling project.
