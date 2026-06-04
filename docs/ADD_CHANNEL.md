# Adding a new channel

A channel is anything where a real person types a message that should reach
the AI: WeChat, browser, future Feishu / Slack / Telegram / email.

## Skeleton

```
agent_bridge/channels/<name>/
├── __init__.py
├── protocol.py    HTTP / WebSocket / SDK calls (whatever the IM uses)
├── state.py       persistent credentials, per-peer state (gitignored)
└── adapter.py     (optional) implement channels.base.Channel
```

State files go under `<channel>_state/` at project root and are gitignored.

## What it must do

1. **Receive messages**: long-poll, websocket, webhook — whatever the IM
   provides. Translate raw payloads into `(peer_id, text, context_dict)`.
2. **Dispatch**:
   - If `cmd.is_command(text)`: call `cmd.handle_command(text)`, reply
     in-channel with the returned string. Do NOT touch the backend.
   - Otherwise: `text = cmd.strip_passthrough_prefix(text)`,
     `alias = sx.get_current()`, write to `inbox_path(alias)`, append to
     history.
3. **Send replies back**: watch every session's `outbox_path(alias)`; when
   fresh content appears, push to the peer who most recently sent into that
   alias. Maintain `alias_peer` mapping so multi-session replies route to
   the right user.
4. **Register in web/server.py**:
   - HTTP routes (status page, OAuth callback, etc.) → `routes/<name>.py`
   - Long-lived tasks (longpoll, watcher) → `web/<name>_runtime.py`
   - Wire into `_lifespan` startup hook so tasks launch on server boot.

## Reference: weixin

`channels/weixin/protocol.py` exposes:

- `get_qrcode(session)` — fetch a QR for first-time login
- `poll_qrcode_status(session, qrcode, base_url)` — watch scan progress
- `get_updates(session, base_url, token, sync_buf)` — long-poll inbox
- `send_text(session, base_url, token, to_user_id, text, context_token)` — outbound
- `extract_text_and_meta(msg)` — translate a raw inbound msg dict into
  `(sender, text, context_token)` or None for non-text payloads.

`channels/weixin/state.py` persists:

- `weixin_state/account.json` — bot_token + base_url + ilink_bot_id
- `weixin_state/context_tokens.json` — per-peer thread context for replies

`web/weixin_runtime.py` runs:

- `_inbound_longpoll(account)` — calls `get_updates` in a loop, dispatches
  messages
- `_outbox_watcher(account)` — walks every session's outbox, forwards
  fresh replies to the right peer
- `qr_login_loop()` — polls scan status during QR onboarding
- `bootstrap_weixin()` — startup hook: resume long-poll if a saved
  account exists

Use the same shape for new channels.

## Channel ABC

`channels/base.py` defines a `Channel` abstract class with `start / stop /
is_connected / send / status` and an `InboundMessage` envelope. The
existing weixin code does NOT inherit from it yet — the ABC documents the
contract, not enforces it. New channels are encouraged to inherit.

## Common gotchas

- **PC WeChat newline rendering**: iLink Bot's Windows desktop client
  collapses multi-line plain text to one line. Mobile clients render
  newlines correctly. Don't try to "fix" this from the bridge side; treat
  PC WeChat as a known-broken renderer.
- **Connection drift**: any longpoll-based channel will see "Server
  disconnected" intermittently. Exponential backoff + retry; don't
  surface to the user.
- **Stale credentials**: distinguish "lost connection, retry" from
  "credentials are dead, re-login needed". For weixin, ret -14 / -2 mean
  the latter — stop long-poll, prompt user to scan again.
