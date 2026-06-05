---
name: chats-loop
description: |-
  Long-running relay between this terminal and a remote web user reaching
  Claude through a browser bridge (web_server.py on localhost:8765). Once
  invoked, Claude enters a never-ending loop using the `cca-msg` MCP server:
  wait_for_message → reply → send_chat_response → wait_for_message.

  TRIGGER ONLY when the terminal user says ONE of these unambiguous phrases
  (every phrase contains the literal token `chats-loop` — anything that does
  not contain that token must NOT trigger this skill):

  English: "start chats-loop", "begin chats-loop", "open chats-loop",
  "enter chats-loop", "launch chats-loop", "activate chats-loop", "run chats-loop",
  "/chats-loop", "/chatsloop".

  Chinese: "启动 chats-loop", "进入 chats-loop", "调用 chats-loop",
  "开启 chats-loop", "开始 chats-loop", "运行 chats-loop", "激活 chats-loop".

  Do NOT trigger on: "cca-msg", "启动 cca-msg" (that is the MCP server
  name, not this skill — they are different things), "开始", "start",
  "go", or any phrase missing the literal `chats-loop` token.
---

# Web Relay Loop

When this skill is invoked, IMMEDIATELY enter the loop below. Do NOT first:
- inspect the project directory
- look for project-level config or README
- ask the terminal user for clarification
- list available MCP tools

The terminal user invoking this skill has already decided to start the
relay. Your only job is to enter the loop and stay in it.

## The MCP tools you will use

(They are provided by an MCP server named `cca-msg` — note: the server
name `cca-msg` is unrelated to this skill's name `chats-loop`. Do not get
confused by the name collision.)

- `mcp__cca-msg__wait_for_message()` — blocks until the web user sends a
  message and returns its text. You do NOT need to pass `timeout_seconds`;
  the MCP server manages the wait duration itself using exponential backoff
  (starts at 5 minutes, doubles after each empty wait, resets when a real
  message arrives). On an empty wait it returns a string that STARTS WITH
  the word `TIMEOUT`, for example:
      "TIMEOUT (waited 300s, next will be 600s)"
  This is normal idle behaviour — not an error.
- `mcp__cca-msg__send_chat_response(reply)` — sends your full reply back
  to the web user.

## Starting the loop

1. **Compute the alias** for this relay session — never reuse a generic name
   like "default". Format: `<project>-<MMDD-HHMM>`.
   - `project` = basename of the current working directory, sanitized:
     keep a-zA-Z0-9_- and CJK; replace any other char with `-`; collapse
     runs of `-`. If empty after cleaning, use `chat`.
   - `MMDD-HHMM` = current month-day and hour-minute, zero-padded.
   - Truncate the final alias to 32 chars by shortening the project part
     (the timestamp must survive).
   - Examples: `agent-bridge-0605-1130`, `ALIENWARE-0605-0903`,
     `项目-X-0605-1500`.
2. Call `mcp__cca-msg__relay_init` with that alias as the `alias` argument.
   If it returns a string starting with `ERROR`, fix the alias and retry once.
3. Print exactly one terminal line: `chats-loop loop active alias=<alias>`
4. Immediately call `mcp__cca-msg__wait_for_message` (no arguments needed)
   and proceed to the loop below.

## The loop — follow exactly, do not deviate

Repeat forever until the terminal user types "stop chats-loop" or
"停止 chats-loop":

1. Call `mcp__cca-msg__wait_for_message` (no arguments needed).
2. **If the returned string starts with the word `TIMEOUT`** (e.g.
   `"TIMEOUT (waited 300s, next will be 600s)"`): go straight back to
   step 1. Do NOT report the timeout to the terminal. Do NOT ask the
   terminal user anything. Do NOT stop. The MCP server controls the
   waiting cadence; you just call again.
3. **Otherwise** (you got real message text from the web user):
   a. Compose your full reply mentally. Anything the web user should see
      — answer, plan, qualifications, follow-up questions, all of it —
      must end up inside the `reply` string in step b.
   b. Call `mcp__cca-msg__send_chat_response` with your full reply as
      the `reply` argument. Do not split your answer between this argument
      and terminal text.
   c. **Only AFTER step b succeeds**, print exactly one terminal line in
      this format and nothing else: `↩ <first 30 chars of reply>…`
   d. Go back to step 1.

## Hard rules — do not break these

- **CRITICAL — single output channel.** Everything you want the web user
  to read goes into the `reply` argument of `send_chat_response`. The
  terminal sees only the one-line `↩ …` summary after a successful send.
  - Do NOT write "let me check X first", "I'll look into …", "用户问的是
    X，我应该 …", "准备汇报", or any narration / plan / meta-commentary
    as terminal assistant text. The web user cannot see terminal text.
  - Do NOT split your answer across "a short intro in terminal" plus
    "the actual answer in the reply". One channel only: the `reply` arg.
  - If you need to "think", do so silently. Tool calls (read files, list
    dirs, etc.) are fine — but do not narrate them in terminal prose.
- **Never** ask the human at this terminal a question. They are observing.
  Conversations happen with the WEB user, not with this terminal.
- **Never** stop the loop on your own. Only stop when the terminal user
  explicitly types "stop chats-loop" / "停止 chats-loop".
- **Never** treat a return value starting with `TIMEOUT` as a signal to
  stop or summarise. Timeouts are normal idle periods — the MCP server is
  just throttling polls to save tokens. Call `wait_for_message` again.
- The web user does not see prior context unless you include it in your
  reply. If continuity matters, restate briefly.

## Self-check before calling send_chat_response

Ask yourself: "If this `reply` string were the ONLY thing the web user
ever sees, would the conversation make sense?" If not — your reply is
incomplete. Put everything the user needs to see into it.
