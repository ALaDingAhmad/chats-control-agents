# agent-bridge

把 IM 渠道（微信，未来飞书 / Slack / …）和 AI 执行后端（Claude Code，未来
OpenClaw / Hermes / …）解耦的插件化桥。

让你在手机上通过微信跟本地 Claude Code 会话聊天。加新渠道：该 IM 也能聊。
加新后端：任何渠道都能跟那个 AI 聊。

## 快速开始

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 装 Claude Code 侧的零件（MCP 服务器、skill、hook）
#    详见 install/README.md
python install/install.py

# 3. 配置 workspace 根（/proj 扫这里）
#    默认 ["D:/aiproject", "F:/wslshare"]，按需改 config.json

# 4. 起 web server
#    端口在 config.json 的 web_port 字段，缺省 8765
python -m chats_control_agents.web.server
# → http://127.0.0.1:<web_port>/
#
# 想关掉终端不影响服务：
#   python -m scripts.start_web_detached   # 后台起，pid 写 web_server.pid
#   python -m scripts.stop_web             # 读 pid 文件、终止
# 仅 Windows。
#
# 改了 web_port 之后：必须重跑 install/install.py --hook，让 hook 副本
# 跟上新端口（hook 不动态读 config，是装的时候渲染一次）

# 5. （可选）连微信
#    打开 http://127.0.0.1:<web_port>/weixin，用手机微信扫码

# 6. 给某个项目开 Claude Code 会话
#    可通过 dashboard 的"开始新会话"按钮、聊天里发 /proj，或手动：
python -m chats_control_agents.backends.claude_code.daemon [<alias>] [<cwd>]
# （alias 省略时自动用 <basename(cwd)>-<MMDD-HHMM>）
```

## 聊天里的命令

| 命令              | 作用                                                |
|------------------|----------------------------------------------------|
| `/proj`          | 列出工作空间下的项目（分页，25/页）                    |
| `/proj more`     | 下一页                                              |
| `<N>`            | （`/proj` 之后）选择项目 #N — 切换 / 拉起             |
| `0`              | （`/proj` 之后）开空会话（cwd=用户主目录、不绑项目） |
| `/list`          | 列出所有会话和状态                                  |
| `/use <alias>`   | 切到指定会话                                        |
| `/new`           | 同 `/proj` — 列项目；回 0 开空会话，回数字开 / 切项目 |
| `/end <alias>`   | 结束会话（60s 内再发一次确认）                       |
| `/rename <new>`  | 重命名当前会话（仅离线时）                          |
| `/help`          | 帮助                                                |
| `//xxx`          | 把 `/xxx` 透传给 AI agent                           |

## 目录结构

```
chats_control_agents/
├── core/         共享逻辑：sessions、commands、router、spawn、history、paths
├── channels/     IM 适配器（weixin；未来 feishu、slack、…）
├── backends/     AI 适配器（claude_code；未来 openclaw、hermes、…）
└── web/          Starlette HTTP 层
docs/
├── 架构.md    总览：数据流 + 模块表 + 会话模型
├── 入站路由.md         入站路由契约（RouteOutcome、idle gate）
├── daemon生命周期.md claude_code daemon 生命周期 + 弹窗处理
├── 新增渠道.md     加新渠道的步骤
└── 新增后端.md     加新后端的步骤
scripts/
├── start_web_detached.py       后台起 web_server（关终端不影响）
├── stop_web.py                 停掉 detached 起的 web_server
└── kill_daemon_children.py     安全杀掉所有后端 spawn 的进程
```

数据流图和模块表见 [`docs/架构.md`](docs/架构.md)。

## 起源

从 [`claude-mcp-bridge`](../claude-mcp-bridge/)（单渠道、单后端）fork 重构而来。
分家前的历史在那个兄弟项目里。
