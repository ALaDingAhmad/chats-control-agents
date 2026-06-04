# Adding a new backend

A backend is whatever turns a user message into a reply: Claude Code TUI
(current), OpenClaw, Hermes, direct Anthropic API, local LLM, …

## Skeleton

```
agent_bridge/backends/<name>/
├── __init__.py
├── adapter.py     (optional) implement backends.base.Backend
└── …              backend-specific files (daemon, RPC client, …)
```

## Two backend shapes

### Process-typed (like claude_code)

- One **daemon process per session** (alias). Daemon spawns the actual
  agent (e.g. `claude.exe`) inside a PTY, keeps it alive, drains output.
- The agent talks to the bridge via **MCP tools** loaded into its host
  process. Tools read/write `chat_sessions/<alias>/inbox.txt` and
  `outbox.txt`.
- Bridge ↔ backend boundary is just files. Loose coupling, robust to
  restarts of either side.

Pros:
- The agent gets a full TUI / interactive environment. Existing tools
  (file ops, shell, MCP servers) work transparently.
- Bridge doesn't need to know anything about the agent's protocol.

Cons:
- Heavy per-session: full TUI process, MCP servers, etc.
- Spawn latency (~5-10s for claude_code).
- Agent state can drift (e.g. rate-limit dialogs blocking the TUI — see
  `daemon.py` rate-limit watchdog).

### API-typed (hypothetical: anthropic_api)

- Stateless HTTP backend. No daemon, no session-bound process. Each
  inbound message turns into a single API call carrying conversation
  history.
- The bridge stores conversation state in `history.json` and replays it
  to the API each turn.

Pros:
- No spawn cost, no process management.
- Easy to scale, easy to reason about.

Cons:
- Bridge has to manage tool calls / loop / history truncation explicitly.
- No interactive TUI; user can't run shell commands through the agent
  without bridge-side glue.

## What it must do

1. **Spawn / connect**: `ensure_session(alias, cwd)` — make sure something
   is ready to answer for this alias. For process-typed: spawn a daemon
   if none is alive. For API-typed: no-op.
2. **Receive messages**: read `inbox_path(alias)`. For process-typed: an
   MCP tool inside the agent process does this. For API-typed: a worker
   inside the backend's adapter does this.
3. **Reply**: write `outbox_path(alias)` in the format
   `"[HH:MM:SS]\n<reply>\n"` so the web `/poll` and weixin outbox watcher
   can pick it up.
4. **Track liveness**: write `meta.json` with `daemon_pid` / `child_pid`
   so `sessions.list_sessions()` can report online state.
5. **Log spawns**: append to `spawned_pids.jsonl` so cleanup tools can
   distinguish backend-spawned processes from user-launched ones (avoids
   the orphan-claude.exe mass-kill bug we hit before).

## Reference: claude_code

`backends/claude_code/daemon.py` does:

- Parse CLI: `python -m agent_bridge.backends.claude_code.daemon <alias> [<cwd>]`
- Spawn `claude.exe --dangerously-skip-permissions` in a `winpty` PTY
- Wait for TUI ready (looks for prompt / box-drawing chars)
- Auto-type `调用 web-relay skill 立即进入消息循环` to kick the skill
- Write `meta.json` and append to `spawned_pids.jsonl`
- Drain loop with watchdog: if PTY shows `You've hit your limit` or
  `/rate-limit-options`, press 3+Enter to dismiss, post a user-facing
  notice via `outbox.txt`, retry the trigger every 5 minutes
- `atexit` cleanup: terminate child, mark meta offline

`backends/claude_code/mcp_bridge.py` is the MCP server the child claude
loads. Exposes two tools:

- `wait_for_message(timeout_seconds=0)` — block on `inbox_path(ALIAS)`
  with 500ms polling; returns the message text when one arrives.
  Internal exponential backoff for empty polls (300s → 600s → …) is
  passed through to the LLM as `TIMEOUT (waited Xs, next will be Ys)`.
- `send_chat_response(reply)` — write `outbox_path(ALIAS)` and return.

The `WEB_RELAY_ALIAS` env var tells mcp_bridge which session it serves;
daemon sets this when spawning the child claude.

## Backend ABC

`backends/base.py` defines `Backend` with `ensure_session / send /
is_session_alive / end_session / session_status`. The existing
claude_code code does NOT inherit yet — the ABC documents the contract.

## Common gotchas

- **PID recycling**: never trust a PID alone. `pid_track.list_daemon_child_pids()`
  cross-checks `psutil.Process(pid).create_time()` against the value
  logged at spawn time. Don't bypass.
- **Don't kill user-launched agent processes**: every cleanup script
  must use `pid_track.list_daemon_descendants()` to scope the kill set.
  The user's own interactive `claude.exe` sessions are *never* in
  `spawned_pids.jsonl`, so this works.
- **Rate limits are real and unrecoverable in-process**: if the upstream
  AI provider rate-limits, the agent process often shows a modal dialog
  that needs keyboard interaction to dismiss. Build the daemon to detect
  and auto-press, otherwise the session locks up indefinitely.
