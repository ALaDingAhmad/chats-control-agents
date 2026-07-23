# agent-bridge — Claude 项目级元规则

> 给 Claude 自己看。一句话定位 + 不在 README/docs 里的约定。
> README → 快速启动 + 命令表 + 布局；`docs/架构.md` → 数据流 + 模块表 + session 模型。
> 本文件只补 Claude 工作时容易踩坑、必须知道但文档不会写的事。

## 一句话定位

把"IM 渠道"和"AI 执行后端"解耦的桥：当前实现 = WeChat (iLink Bot) ↔ Claude Code（channels 推模型，backend=`claude_channel`）。
`channels/` 和 `backends/` 是插件位；加新渠道/后端 = 加一个文件。

## ⚠️ 已删除：claude_code backend + chats-loop 全套（2026-07-23）

**`claude_code` backend 连同 mcp_bridge / cca-msg MCP / chats-loop skill+hook / bridge-owned 会话模型，全部删除。** 现在只有 `claude_channel`（默认）和 `hermes_acp` 两个 backend。历史原因见下，避免下次会话又抱着这套旧东西：

- 老模型（claude_code）：daemon spawn child claude（TUI），child 靠 **mcp_bridge.py 提供的 cca-msg MCP 工具**（`wait_for_message` / `send_chat_response` / `relay_init`）读写 inbox/outbox，用户也可手开 claude 跑 **`/chats-loop` skill** 成为 "bridge-owned" 会话。
- 被取代：`claude_channel` 用 Claude Code channels 推模型，daemon **直接读写 inbox/outbox**，不需要 mcp_bridge、cca-msg、chats-loop skill。手开会话要接回也走 **resume**（`--resume` transcript），不存在 "bridge-owned 接管" 这回事。
- 因此凡是 `mcp_bridge`、`cca-msg`、`chats-loop`、`bridge_pid`、`bridge-owned`、`CHATS_LOOP_ALIAS`(env 名保留但仅作 alias 通道) 相关的旧逻辑，若还在 git 历史/老 handoff 里看到，一律视为**已废弃**，以本文件和 docs 为准。
- 就绪 marker 从 `.chats-loop-active-<alias>` **改名为 `.session-ready-<alias>`**（channel/hermes 的就绪租约信号，与已删的 chats-loop skill 无关，改名去掉误导字样）。

## 关键架构硬约束（动代码前必须知道）

- **包名是 `chats_control_agents`，不是 `agent_bridge`**。后者只活在 2026-06-04 之前的 git 历史和那一份 handoff 里。所有路径、import、`python -m ...` 命令全用 `chats_control_agents`。
- **`paths.ROOT = Path(__file__).resolve().parents[2]`**，必须从 `chats_control_agents/core/paths.py` 这个相对位置算。
  - 任何新文件想算 ROOT，**直接 `from chats_control_agents.core.paths import ROOT`**，不要自己再 `Path(__file__).parent...`。`channels/weixin/state.py` 就是栽在这上面的（详见 06-04 handoff）。
  - `mcp_bridge.py` 例外：它要支持脱离包被裸跑（被 child claude 当 stdio 服务器拉起时 sys.path 不含 ROOT），所以 ROOT 是手算的 `parents[3]` + 自插 sys.path。这是已知的脆弱点，复制粘贴时小心。
- **alias 规则：`<basename(cwd)>-<MMDD-HHMM>`**，由 `core.sessions.make_alias_for_cwd(cwd)` 生成。`default` 这个 alias 已废弃（commit `167f1d9`），不要在新代码里写死它。老 `chat_sessions/default/` 目录留着只是为了向后读，不要复用。
- **alias 传递走 env 变量 `CHATS_LOOP_ALIAS`**。daemon spawn child claude 时设这个 env。（名字带 LOOP 是历史遗留，现在只是纯 alias 通道，与已删的 chats-loop 无关。）
- ~~MCP 服务器 `cca-msg` / Skill `chats-loop` / `mcp_bridge.py`~~ —— **已全部删除**（见顶部"已删除"段）。`~/.claude.json` 里的 `mcpServers.cca-msg` 注册也已清。
- **全局单选中 alias**：`chat_sessions/_current.txt` 一个文件，所有 weixin/web 入站消息都路由到它。多用户路由要按 `peer_id` 分流，目前没做。
- **daemon spawn child claude 的 cwd 不是 agent-bridge 自己**，而是 `D:/aiproject/claude-code-account-switch`（ccs 工具目录），这样 child claude 用 CCS 当前选中的账号。不要假定 child claude 跟 daemon 在同一目录。
- **现在有两个 backend：`claude_channel`（默认）、`hermes_acp`**。`meta.json.backend` 字段决定起哪个 daemon（`core.spawn._resolve_daemon_module`）。命令行入口（微信 `/proj` / `/new`）读 `chat_sessions/_default_backend.txt`（**缺省 `claude_channel`**），`/backend <name>` 命令改它；dashboard 建会话用 modal 下拉选，不读此文件。**新加 backend 要同步 3 处**：`core.sessions.KNOWN_BACKENDS`、`core.spawn._BACKEND_DAEMON_MODULES`、`web.spawn_helpers._KNOWN_BACKENDS`。详见 `docs/后端设计.md`。
- **只有 `claude_channel` 支持 resume（接回历史会话）**。`/proj` 语义已翻转为"选项目→选该项目历史会话→接回上下文"（default backend=claude_channel 时才走两级菜单，否则退化直接起会话）；`/new` 是开全新白纸的显式出口。通路：router 写 `RESUME:<session-id>` 进 `pty_control.txt`，claude_channel daemon 主循环认 `RESUME:` 前缀 → kill 旧 child → 带 `--resume` 重 spawn。`--resume` 与 dev-channel flag 实测兼容。详见 `docs/入站路由.md` "两级菜单" + `docs/后端设计.md` "resume 控制通路"。
- **web 端口在 `config.json:web_port`**，缺省 8765。代码里**不**写死端口——`core.config.get_web_port()` 是单一来源，server / dashboard UI / start_web_detached 都读它。（原先的 `chats_loop_pretool_hook.py` 端口渲染例外已随 chats-loop hook 删除一并消失。）

## 已知坑（容易再踩）


- **`_wx["alias_peer"]` 持久化到 `weixin_state/alias_peer.json`**：inbound 收到消息时 `wxs.set_alias_peer(alias, sender)` 落盘，`start_runtime_tasks` 启动时 `load_alias_peer()` 回填进 `_wx`。web_server 重启后第一条 outbox→weixin 不再丢。如果想"忘掉"某 alias 的 peer 映射（比如换号），删 `weixin_state/alias_peer.json` 里对应 key 即可。
- **`_outbox_seen` 也没持久化，但有 prime 兜底**：watcher 启动时会扫所有 alias 的 outbox.txt 内容预记入 `_outbox_seen`（不发出，只视为"已见"）。这阻止了"web /send 测试残留在 outbox → weixin 接入后被当新消息重放"。**不要去掉 prime 逻辑**——`weixin_runtime.py` `_outbox_watcher` 函数开头。
- **outbox_watcher 失败重试策略**：send_text 失败时**不 mark seen**，下个 0.5s 循环会重试。临时网络/token 抖动会自愈，但永久错误会刷屏日志——这是有意的，让你看见而不是闷死。
- **PC 微信换行被吞**：iLink 协议层把 `\n` 在 PC 客户端压成一行（手机正常），无解决方案。多行输出（如 `/list`、`/proj`）只在手机上排版正确，**测试 UX 必须用手机微信看**，不要拿 PC 微信判断"格式对不对"。
- **`send_text failed:` 不要默认是网络错误**。06-08 上午两次失败是 token 短暂异常；但 outbox_watcher 当前在 send 失败后**仍然 mark seen**（`weixin_runtime.py` L442 在 try/except 外面），导致一次失败 = 永久丢消息。改之前想清楚：永久错误重试会刷屏，临时错误不重试会丢。
- **outbox 是直接覆写 outbox.txt 而不是 append**（channel daemon `_write_outbox`）。这是设计：outbox 是"最新一条待推送的回复"，不是历史。watcher 用 `[stamp]|reply[:120]` 当指纹去重。如果未来要支持一次回多条，整个模型要重做。
- **marker（`.session-ready-<alias>`）判在线看 mtime 新鲜度，不看文件存在**：marker 被硬杀进程留残留（atexit 不跑）。真判据是 mtime 在 TTL（`paths.LOOP_MARKER_TTL_SECS` 180s）内。channel/hermes daemon 就绪后 touch 它，`spawn.watch_ready` 靠它判就绪。在线判定 = `daemon 活`（channel 会话唯一活法；bridge-owned 那套已删）。

## 进程模型（debug 时要心里有数）

一个活动会话有 3 层进程，三者父子关系是嵌套的：

```
web_server.py (Starlette, port = config.json:web_port，缺省 8765)
  └─ subprocess: daemon.py (per alias, detached; backend=claude_channel)
       └─ PtyProcess: claude.exe (child claude, cwd=ccs 目录)
```

daemon 直接读写 `chat_sessions/<alias>/inbox.txt` / `outbox.txt`，不再有 mcp_bridge 子进程（claude_code + cca-msg 已删）。

- web_server 死了：daemon + child claude + mcp_bridge 都还活着（detached），重启 web_server 不影响会话。
- daemon 死了：child claude + mcp_bridge 还活着，但没人 watchdog。`web.spawn_helpers.ensure_daemon_alive` 在 weixin inbound 时会检查并 respawn 一个新 daemon——但新 daemon 会 spawn 一个**新的** child claude，老的孤儿不会被回收。debug 时 `ps` 看到多个同 alias 的 claude.exe 就是这种情况。
- child claude 死了：daemon watchdog 会发现并通过 outbox 写"撞限额"提示。

## Bash on Windows 注意

- `pty.log` 经常上百 MB，**别 `cat`**，用 `tail -200` 或 `Read offset=...`。
- `chat_sessions/` 下文件名可以含中文（alias 支持 CJK），写 shell 命令时记得加双引号。
- 默认 Git Bash 路径用正斜杠；`python -m chats_control_agents.web.server` 这种命令两边 shell 都行。

## 配置版本

- **2026-07-23：删除 claude_code backend + chats-loop 全套**（见顶部"已删除"段）。删 `backends/claude_code/`（daemon+mcp_bridge）、`install/skills/chats-loop/`、`install/hooks/chats_loop_pretool_hook.py`；摘掉三处 backend 注册表里的 claude_code；默认 backend 兜底 `claude_code`→`claude_channel`；清 `~/.claude.json` 的 `mcpServers.cca-msg`；就绪 marker `.chats-loop-active-`→`.session-ready-`。下方 07-16 那条 bridge-owned 契约**已作废**。
- 2026-07-21：claude_channel resume（接回历史会话）落地。`/proj` 翻转为 resume 默认路径（两级菜单：项目→会话），`/new` 为白纸出口。新增 `core/resume_choices.py`（第二级 arm 令牌）+ `core/resume_scan.py`（扫 `~/.claude/projects/<cwd>/*.jsonl` 列最近5会话、清洗首条人话摘要）。daemon 主循环认 `RESUME:` 前缀 kill+带 `--resume` 重 spawn。本地 e2e 全过（含上下文真接回验证）。详见两份 docs 上述专节。
- ~~2026-07-16：bridge-owned 会话契约落地~~ —— **已于 2026-07-23 随 claude_code/chats-loop 一并删除，作废。**
- 2026-06-22：纯数字入站语义改 one-shot 菜单选择（详见 `docs/入站路由.md` "纯数字入站"段）。废掉了 daemon **全程常开的 `control_mode`**——以前 `/proj` 120s 窗口外任何数字都被当 PTY 控制吞掉。现在数字默认是聊天，只有菜单刚弹出那一回合才认。daemon `write_menu_block` 拆成"普通文本纯中继 / 菜单才 arm+脚注"（`_looks_like_menu` heuristic）。
- 最后更新：2026-06-08（创建后同日修了 outbox 残留重放 bug，已同步"已知坑"那段）
- 起因：会话恢复后从老 handoff（提到 `agent_bridge` 包名、`WEB_RELAY_ALIAS` env、`web-chat` MCP 名）转过来，发现 06-04 之后的 14 个 commit 把这些都改了。沉淀到本文件，避免下次会话再被老 handoff 误导。
