# Installer for the artefacts that live outside the repo

agent-bridge has three pieces that need to be visible to Claude Code globally
(in your home dir), not just to the project:

| Component | Lives at | What it does |
|---|---|---|
| **MCP server `cca-msg`** | `~/.claude.json → mcpServers.cca-msg` | Registers `mcp_bridge.py` so every Claude window exposes the `wait_for_message` / `send_chat_response` / `relay_init` tools |
| **Skill `chats-loop`** | `~/.claude/skills/chats-loop/` | Tells Claude how to enter the relay loop when you say "start chats-loop" |
| **PreToolUse hook** | `~/.claude/hooks/chats_loop_pretool_hook.py` + a matcher entry in `~/.claude/settings.json` | Mirrors Claude's narration text to the browser before each `send_chat_response` |

This installer copies them in, with backups, and is idempotent.

## Quick start

```bash
# install all three components
python install/install.py

# preview without writing anything
python install/install.py --dry-run

# only one component
python install/install.py --mcp
python install/install.py --skill
python install/install.py --hook

# reverse it
python install/install.py --uninstall
```

## What it touches

- `~/.claude.json` — adds/updates the `mcpServers.cca-msg` entry. **Backup
  taken first** as `~/.claude.json.bak-<timestamp>`. All other MCP servers
  and top-level fields are preserved (merge, not overwrite).
- `~/.claude/settings.json` — adds a `PreToolUse` entry matched on
  `mcp__cca-msg__send_chat_response`. Backup taken first.
- `~/.claude/skills/chats-loop/` — copies `SKILL.md`. If a different version
  is already there it's moved to `chats-loop.bak-<timestamp>/`.
- `~/.claude/hooks/chats_loop_pretool_hook.py` — copies the script. Same
  backup rule.

## After installing

Anything that's *already running* needs a restart to pick up changes:

- **Claude windows**: each one loads `~/.claude.json` once at startup, so
  the new MCP path takes effect only after restarting that window.
- **web_server** (`python -m chats_control_agents.web.server`): the dashboard and
  `/session/new` route are part of the repo, not the installer, but the
  running web_server has to be restarted separately to pick up code
  changes from the repo.

## Path portability

`install.py` derives `mcp_bridge.py`'s absolute path from its own location
(`<repo>/install/install.py`), so cloning the repo to a different drive and
running the installer works without editing anything.
