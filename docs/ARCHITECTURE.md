# agent-bridge architecture

A pluggable bridge between **IM channels** (where humans type) and **AI
execution backends** (what answers). Originally built to expose a local
Claude Code TUI to a phone via WeChat; now generalized so adding Feishu or
OpenClaw is just a new file under `channels/` or `backends/`.

## Data flow

```
┌─────────┐  inbound text   ┌──────────┐   /send + sx.is_command?  ┌──────────┐
│ WeChat  │ ───────────────▶│          │ ──────────────────────────▶│ commands │  ──▶ reply
│ Browser │                 │   web    │                            │   (/list,│
│ Feishu? │ ◀───────────────│  routes  │ ◀──────────────────────────│   /proj) │
└─────────┘  outbound reply └──────────┘   reply text               └──────────┘
                                │
                                │ regular message → write inbox.txt
                                ▼
                      ┌─────────────────┐
                      │ chat_sessions/  │
                      │   <alias>/      │
                      │     inbox.txt   │ ◀── polled by mcp_bridge
                      │     outbox.txt  │ ──▶ polled by web /poll + outbox_watcher
                      │     history.json│
                      │     meta.json   │
                      └─────────────────┘
                                ▲
                                │ inbox poll / outbox write
                                │
                      ┌─────────────────┐
                      │  claude_code    │  ◀── daemon spawns child claude.exe
                      │   backend       │       with CHATS_LOOP_ALIAS=<alias>
                      │ (process-typed) │       child loads mcp_bridge as MCP
                      └─────────────────┘       server, calls wait_for_message
```

## Package layout

```
chats_control_agents/
├── core/                  cross-cutting: no IO concerns, no backend specifics
│   ├── paths.py           ROOT, SESSIONS_ROOT, CONFIG_FILE, ALIAS_RE, path helpers
│   ├── config.py          load/save_config, get_workspace_roots
│   ├── sessions.py        get/set_current, list_sessions, load/save_meta_for,
│   │                      migrate_legacy_if_present
│   ├── projects.py        list_projects (workspace scan + alias cross-ref)
│   ├── proj_choices.py    persistent /proj selection state
│   ├── autospawn.py       request_autospawn (writes to queue)
│   ├── pid_track.py       _pid_alive (cross-platform), daemon child tracking
│   └── commands.py        is_command, handle_command, all _cmd_*
│
├── channels/              "user-facing end"
│   ├── base.py            Channel ABC + InboundMessage envelope
│   └── weixin/            iLink Bot
│       ├── protocol.py    HTTP API (QR login, longpoll, send_text)
│       └── state.py       token + per-peer context_token persistence
│
├── backends/              "AI execution end"
│   ├── base.py            Backend ABC
│   └── claude_code/       spawns child claude per session
│       ├── daemon.py      PtyProcess spawn + watchdog (rate-limit dismiss)
│       └── mcp_bridge.py  MCP server: wait_for_message + send_chat_response
│
└── web/                   Starlette HTTP layer
    ├── server.py          app assembly + lifespan
    ├── helpers.py         load/save_history
    ├── weixin_runtime.py  _wx state, QR loop, longpoll, outbox watcher
    ├── autospawn.py       autospawn_worker
    ├── routes/
    │   ├── chat.py        / /history /send /poll /relay-push
    │   ├── sessions.py    /sessions
    │   ├── projects.py    /projects /config /config/workspace
    │   └── weixin.py      /weixin/* (status, qr/start, disconnect)
    └── templates/
        ├── index.html
        └── weixin.html
```

## Session model

A *session* is `chat_sessions/<alias>/`. Files inside:

| file | who writes | who reads |
|------|-----------|-----------|
| `inbox.txt`          | channel inbound paths (web /send, weixin longpoll) | backend's mcp_bridge `wait_for_message` |
| `outbox.txt`         | backend's mcp_bridge `send_chat_response`           | web /poll + weixin outbox watcher    |
| `history.json`       | web routes (append on each turn)                    | UI                                   |
| `meta.json`          | daemon (start/exit) + commands (`/new`)             | sessions.list_sessions, projects.py |
| `spawned_pids.jsonl` | daemon (append per spawn)                           | pid_track.list_daemon_child_pids    |
| `daemon.log` `pty.log` `daemon_stdout.log` | daemon                          | humans                               |

## Currently-selected alias

`chat_sessions/_current.txt` holds a single alias. All channel-inbound
regular messages route to *this* alias's inbox. Slash commands like
`/use <alias>` change it. Single global selection — works because
agent-bridge is currently single-user; multi-user routing would need to
key on `peer_id` instead.

## /proj numeric pick flow

After `/proj` outputs a numbered listing, the user can reply with a bare
integer to select. State (the list and an expiration) is persisted to
`chat_sessions/_pending_proj.json` so it survives web_server restarts —
the user's `/proj` may have come from a previous process lifetime.

## Autospawn

When `/proj` picks an offline / not-yet-existing project,
`core.autospawn.request_autospawn(alias, cwd)` appends to
`chat_sessions/_autospawn_queue.jsonl`. The web server's
`web.autospawn.autospawn_worker` background task drains it and spawns
`python -m chats_control_agents.backends.claude_code.daemon <alias> <cwd>`
detached (Windows: `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`).

## Channel ↔ backend ↔ command interactions

- **Slash command** (`/proj`, `/list`, …): `cmd.is_command(text)` returns
  True; the channel handler invokes `cmd.handle_command(text)` and replies
  directly. Never touches the backend.
- **Passthrough** (`//handoff`, `//recall`): `is_command` returns False;
  `strip_passthrough_prefix` drops one slash so the agent sees `/handoff`.
- **Regular message**: written to `inbox.txt` of `sx.get_current()`. The
  backend's adapter (e.g. claude_code's mcp_bridge inside child claude)
  picks it up.

## Adding a channel / backend

See `docs/ADD_CHANNEL.md` and `docs/ADD_BACKEND.md` for step-by-step.
