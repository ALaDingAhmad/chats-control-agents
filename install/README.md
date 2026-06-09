# 仓库外那些零件的安装器

agent-bridge 有三个零件需要 Claude Code 全局可见（在你的家目录里），不能只
留在项目里：

| 组件 | 放在哪 | 干啥的 |
|---|---|---|
| **MCP 服务器 `cca-msg`** | `~/.claude.json → mcpServers.cca-msg` | 注册 `mcp_bridge.py`，让每个 Claude 窗口都能用 `wait_for_message` / `send_chat_response` / `relay_init` 工具 |
| **Skill `chats-loop`** | `~/.claude/skills/chats-loop/` | 告诉 Claude 听到"start chats-loop"时怎么进入中继循环 |
| **PreToolUse hook** | `~/.claude/hooks/chats_loop_pretool_hook.py` + `~/.claude/settings.json` 里的匹配项 | 在每次 `send_chat_response` 之前把 Claude 的旁白文本镜像到浏览器 |

这个安装器会拷进去（带备份），幂等可重跑。

## 快速开始

```bash
# 装三个组件
python install/install.py

# 干跑预览，不真写
python install/install.py --dry-run

# 只装一个
python install/install.py --mcp
python install/install.py --skill
python install/install.py --hook

# 反装
python install/install.py --uninstall
```

## 会改动哪些文件

- `~/.claude.json` —— 加/更新 `mcpServers.cca-msg` 项。**先备份**为
  `~/.claude.json.bak-<时间戳>`。其它 MCP 服务器和顶层字段都保留（合并，
  不是覆盖）。
- `~/.claude/settings.json` —— 加 `PreToolUse` 项，匹配
  `mcp__cca-msg__send_chat_response`。先备份。
- `~/.claude/skills/chats-loop/` —— 拷 `SKILL.md`。已有不同版本会移到
  `chats-loop.bak-<时间戳>/`。
- `~/.claude/hooks/chats_loop_pretool_hook.py` —— 拷脚本。同样的备份规则。

## 装完之后

**正在跑的东西**要重启才能加载新东西：

- **Claude 窗口**：每个窗口启动时只读一次 `~/.claude.json`，新 MCP 路径要
  重启该窗口才生效。
- **web_server**（`python -m chats_control_agents.web.server`）：dashboard 和
  `/session/new` 路由是仓库的事不是安装器的事，但正在跑的 web_server 仍要
  单独重启才能加载仓库里的代码改动。

## 路径可移植性

`install.py` 从自身位置（`<repo>/install/install.py`）算出 `mcp_bridge.py`
的绝对路径，所以把仓库 clone 到另一个盘上跑安装器无需改任何东西。
