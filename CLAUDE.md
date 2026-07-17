# agent-bridge — Claude 项目级元规则

> 给 Claude 自己看。一句话定位 + 不在 README/docs 里的约定。
> README → 快速启动 + 命令表 + 布局；`docs/ARCHITECTURE.md` → 数据流 + 模块表 + session 模型。
> 本文件只补 Claude 工作时容易踩坑、必须知道但文档不会写的事。

## 一句话定位

把"IM 渠道"和"AI 执行后端"解耦的桥：当前实现 = WeChat (iLink Bot) ↔ Claude Code TUI。
`channels/` 和 `backends/` 是插件位；加新渠道/后端 = 加一个文件。

## 关键架构硬约束（动代码前必须知道）

- **包名是 `chats_control_agents`，不是 `agent_bridge`**。后者只活在 2026-06-04 之前的 git 历史和那一份 handoff 里。所有路径、import、`python -m ...` 命令全用 `chats_control_agents`。
- **`paths.ROOT = Path(__file__).resolve().parents[2]`**，必须从 `chats_control_agents/core/paths.py` 这个相对位置算。
  - 任何新文件想算 ROOT，**直接 `from chats_control_agents.core.paths import ROOT`**，不要自己再 `Path(__file__).parent...`。`channels/weixin/state.py` 就是栽在这上面的（详见 06-04 handoff）。
  - `mcp_bridge.py` 例外：它要支持脱离包被裸跑（被 child claude 当 stdio 服务器拉起时 sys.path 不含 ROOT），所以 ROOT 是手算的 `parents[3]` + 自插 sys.path。这是已知的脆弱点，复制粘贴时小心。
- **alias 规则：`<basename(cwd)>-<MMDD-HHMM>`**，由 `core.sessions.make_alias_for_cwd(cwd)` 生成。`default` 这个 alias 已废弃（commit `167f1d9`），不要在新代码里写死它。老 `chat_sessions/default/` 目录留着只是为了向后读，不要复用。
- **alias 传递走 env 变量 `CHATS_LOOP_ALIAS`**（不是老 handoff 里写的 `WEB_RELAY_ALIAS`）。daemon spawn child claude 时设这个 env，child claude 再 spawn mcp_bridge 子进程时自动继承。
- **MCP 服务器名 = `cca-msg`**（不是 `web-chat`）。Skill 触发器名 = `chats-loop`（不是 `web-relay`）。`~/.claude.json` 里 `mcpServers.cca-msg.args[0]` 必须指向本仓库的 `chats_control_agents/backends/claude_code/mcp_bridge.py`。改路径前先备份 `~/.claude.json`。
- **全局单选中 alias**：`chat_sessions/_current.txt` 一个文件，所有 weixin/web 入站消息都路由到它。多用户路由要按 `peer_id` 分流，目前没做。
- **daemon spawn child claude 的 cwd 不是 agent-bridge 自己**，而是 `D:/aiproject/claude-code-account-switch`（ccs 工具目录），这样 child claude 用 CCS 当前选中的账号。不要假定 child claude 跟 daemon 在同一目录。
- **现在有两个 backend：`claude_code` 和 `hermes_acp`**。`meta.json.backend` 字段决定起哪个 daemon（`core.spawn._resolve_daemon_module`）。命令行入口（微信 `/proj` / `/new`）读 `chat_sessions/_default_backend.txt`（缺省 `claude_code`），`/backend <name>` 命令改它；dashboard 建会话用 modal 下拉选，不读此文件。**新加 backend 要同步 3 处**：`core.sessions.KNOWN_BACKENDS`、`core.spawn._BACKEND_DAEMON_MODULES`、`web.spawn_helpers._KNOWN_BACKENDS`。详见 `docs/BACKEND-DESIGN.md`。
- **web 端口在 `config.json:web_port`**，缺省 8765。代码里**不**写死端口——`core.config.get_web_port()` 是单一来源，server / dashboard UI / start_web_detached 都读它。**例外**：hook 副本 `~/.claude/hooks/chats_loop_pretool_hook.py` 是 install.py 装的时候渲染一次（源文件里 `# CHATS_BRIDGE_WEB_PORT_LINE` 标记行被替换）。**改了 `web_port` 必须重跑 `python install/install.py --hook`**——否则 hook 还会 push 到老端口。

## 已知坑（容易再踩）

- **cca-msg 注册禁止用裸 `python`**：`~/.claude.json` 的 `mcpServers.cca-msg.command` 必须写绝对路径（如 `C:\Users\ALIENWARE\AppData\Local\Microsoft\WindowsApps\python3.11.exe`）。裸 `python` 按启动 claude 的 shell 的 PATH 解析——在激活了 venv 的终端里会被劫持到项目 venv（没有 mcp/psutil），mcp_bridge import 秒崩，Claude Code 报 `-32000 connection closed`，且 `mcp_bridge.log` 里毫无痕迹（崩在 logging 初始化之前）。2026-07-17 在 `F:\wslshare\analyze`（VIRTUAL_ENV 激活）实测踩中。

- **`_wx["alias_peer"]` 持久化到 `weixin_state/alias_peer.json`**：inbound 收到消息时 `wxs.set_alias_peer(alias, sender)` 落盘，`start_runtime_tasks` 启动时 `load_alias_peer()` 回填进 `_wx`。web_server 重启后第一条 outbox→weixin 不再丢。如果想"忘掉"某 alias 的 peer 映射（比如换号），删 `weixin_state/alias_peer.json` 里对应 key 即可。
- **`_outbox_seen` 也没持久化，但有 prime 兜底**：watcher 启动时会扫所有 alias 的 outbox.txt 内容预记入 `_outbox_seen`（不发出，只视为"已见"）。这阻止了"web /send 测试残留在 outbox → weixin 接入后被当新消息重放"。**不要去掉 prime 逻辑**——`weixin_runtime.py` `_outbox_watcher` 函数开头。
- **outbox_watcher 失败重试策略**：send_text 失败时**不 mark seen**，下个 0.5s 循环会重试。临时网络/token 抖动会自愈，但永久错误会刷屏日志——这是有意的，让你看见而不是闷死。
- **PC 微信换行被吞**：iLink 协议层把 `\n` 在 PC 客户端压成一行（手机正常），无解决方案。多行输出（如 `/list`、`/proj`）只在手机上排版正确，**测试 UX 必须用手机微信看**，不要拿 PC 微信判断"格式对不对"。
- **`send_text failed:` 不要默认是网络错误**。06-08 上午两次失败是 token 短暂异常；但 outbox_watcher 当前在 send 失败后**仍然 mark seen**（`weixin_runtime.py` L442 在 try/except 外面），导致一次失败 = 永久丢消息。改之前想清楚：永久错误重试会刷屏，临时错误不重试会丢。
- **send_chat_response 是直接覆写 outbox.txt 而不是 append**。这是设计：outbox 是"最新一条待推送的回复"，不是历史。watcher 用 `[stamp]|reply[:120]` 当指纹去重。如果未来要支持一次回多条，整个模型要重做。
- **手开的 claude.exe 不会污染会话**：commit `167f1d9` 之后，没设 `CHATS_LOOP_ALIAS` env 的 mcp_bridge.py 会用 `<basename(cwd)>-<MMDD-HHMM>` 拿独立 alias，不抢 daemon-spawned session 的 inbox。所以 `ps` 看到的额外几个 mcp_bridge.py 进程是无害的，不要顺手杀。**但 mcp_bridge 绝不能写 `_current.txt`**——2026-07-16 一版半成品在 bridge 启动时抢路由指针，导致随手开个 claude 窗口就劫持全部微信入站、消息落进没人消费的 inbox 全网静默。"成为当前会话"只能靠用户显式选择。相关契约见 `docs/ROUTING.md` "终端 chats-loop 会话（bridge-owned）"。
- **"bridge 进程活着" ≠ "有人在收件"，"marker 文件在" ≠ "循环在跑"**：bridge 是 claude 连上 MCP 就有的；marker 被硬杀进程留残留（atexit 不跑）。真判据是 marker 的 **mtime 新鲜度**（wait 期间每 ~5s touch 的心跳租约，TTL=`paths.LOOP_MARKER_TTL_SECS` 180s）。任何在线/可服务判定都用 `daemon 活 || (bridge 活 && paths.loop_marker_fresh(alias))`，不许只查 bridge PID 或 marker 存在性。

## 进程模型（debug 时要心里有数）

一个活动会话有 3 层进程，三者父子关系是嵌套的：

```
web_server.py (Starlette, port = config.json:web_port，缺省 8765)
  └─ subprocess: daemon.py (per alias, detached)
       └─ PtyProcess: claude.exe (child claude, cwd=ccs 目录)
            └─ subprocess: mcp_bridge.py (stdio, env CHATS_LOOP_ALIAS=<alias>)
```

- web_server 死了：daemon + child claude + mcp_bridge 都还活着（detached），重启 web_server 不影响会话。
- daemon 死了：child claude + mcp_bridge 还活着，但没人 watchdog。`web.spawn_helpers.ensure_daemon_alive` 在 weixin inbound 时会检查并 respawn 一个新 daemon——但新 daemon 会 spawn 一个**新的** child claude，老的孤儿不会被回收。debug 时 `ps` 看到多个同 alias 的 claude.exe 就是这种情况。
- child claude 死了：daemon watchdog 会发现并通过 outbox 写"撞限额"提示。

## Bash on Windows 注意

- `pty.log` 经常上百 MB，**别 `cat`**，用 `tail -200` 或 `Read offset=...`。
- `chat_sessions/` 下文件名可以含中文（alias 支持 CJK），写 shell 命令时记得加双引号。
- 默认 Git Bash 路径用正斜杠；`python -m chats_control_agents.web.server` 这种命令两边 shell 都行。

## 配置版本

- 2026-07-16：bridge-owned 会话契约落地（`docs/ROUTING.md` 新增专节）。mcp_bridge 只注册 meta（bridge_pid），不抢 `_current.txt`；路由可服务性 = `daemon 活 || (bridge 活 && marker 在)`，bridge 活但 marker 不在 → 回 `/proj` 菜单而不是静默吞消息。
- 2026-06-22：纯数字入站语义改 one-shot 菜单选择（详见 `docs/ROUTING.md` "纯数字入站"段）。废掉了 daemon **全程常开的 `control_mode`**——以前 `/proj` 120s 窗口外任何数字都被当 PTY 控制吞掉。现在数字默认是聊天，只有菜单刚弹出那一回合才认。daemon `write_menu_block` 拆成"普通文本纯中继 / 菜单才 arm+脚注"（`_looks_like_menu` heuristic）。
- 最后更新：2026-06-08（创建后同日修了 outbox 残留重放 bug，已同步"已知坑"那段）
- 起因：会话恢复后从老 handoff（提到 `agent_bridge` 包名、`WEB_RELAY_ALIAS` env、`web-chat` MCP 名）转过来，发现 06-04 之后的 14 个 commit 把这些都改了。沉淀到本文件，避免下次会话再被老 handoff 误导。
