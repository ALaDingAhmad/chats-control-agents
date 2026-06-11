# Backend 设计原则

> 2026-06-10 立。决定 agent-bridge 怎么接第二个 backend（hermes_acp），
> 同时把"为什么 backend 长成现在这样"沉淀下来，避免下一个 backend 重新争论。

读这份之前，先读 `ARCHITECTURE.md` 了解 channel/router/文件协议三层。

## 一句话

**Backend 是"消费 inbox.txt → 产出 outbox.txt"的黑盒**。怎么消费、怎么产出，是
backend 自己的实现细节。router、channel、web 都不关心。

## 文件协议是 backend 唯一的契约

router 写 `chat_sessions/<alias>/inbox.txt`，web/channel 读
`chat_sessions/<alias>/outbox.txt`。这两个文件 + `meta.json` + `history.json`
是 backend 跟 agent-bridge 其它部分通信的**全部接口**。

| 文件 | router 视角 | backend 视角 |
|---|---|---|
| `inbox.txt` | 写：用户消息 | 读：拿到要处理的消息 |
| `outbox.txt` | 读：拿回复推给用户 | 写：覆写最新的"待推送回复" |
| `meta.json` | 读：`daemon_pid` 判活、`backend` 字段选 daemon 模块 | 写：alias / cwd / 各种 pid / created_at |
| `history.json` | 写：每轮 user / assistant 追加 | 不读不写 |
| `spawned_pids.jsonl` | 不读 | 写：daemon 自己 spawn 的子进程 PID + create_time |

**协议特性**（落 backend 前必读）：

- `outbox.txt` 是**覆写**，不是追加 —— "最新一条待推送的回复"，watcher 用
  `[stamp]|reply[:120]` 当指纹去重。要支持流式回复必须升协议（见后文）。
- `inbox.txt` 同样是覆写，由 router 在新消息到来时写入；backend 读完后不要清空
  —— 让 watcher 用 mtime 判新。
- `meta.json` 必填字段：`alias`、`cwd`、`daemon_pid`、`child_pid`、`created_at`、
  `backend`（新增）。dead 之后追加 `last_exit_at` 并把两个 pid 清成 null。
- `last_exit_at` 两种来源：(a) daemon 自己 atexit 钩子写的 ISO 时间戳——正常退出；
  (b) `"(detected_dead)"`——`core.sessions.list_sessions` 扫描时发现 meta 字面声称
  在线但 PID 已不活时的 lazy-fix 标记（被 `taskkill /F` / OOM / 解释器崩等绕过
  atexit 时会落这种）。读 meta 的人不需要区分这两种，只关心"有这字段 = 已离线"。

## daemon 在消息路径上吗？由 backend 决定

这是设计 backend 时最关键的一个判断，**没有"对"的答案**，取决于下游 agent 的协议形态：

### 路径外（claude_code 是这种）

```
router → inbox.txt
             ↑ poll
        ┌────┴────┐
        │ child claude（持有 mcp_bridge）  ← daemon spawn 它，但不管消息
        └────┬────┘
             ↓ write
         outbox.txt → web/channel
```

- daemon 只负责"让 child claude 一直活着且处于 wait_for_message 状态"
- 消息流转完全发生在 daemon 之外（child claude 内嵌的 mcp_bridge 自己 IO 文件）
- daemon 崩了 child claude 还能继续干活；下次 inbound 时 router 会 respawn 一个 daemon 接管看护

### 路径内（hermes_acp 是这种）

```
router → inbox.txt
             ↑ poll
        ┌────┴────┐
        │ daemon (持有 ACP stdio socket)
        └────┬────┘
             │ session/prompt → ACP → session/update chunks
             ↓ aggregate + write
         outbox.txt → web/channel
```

- daemon 自己 poll inbox、跟下游 agent 走 stdio JSON-RPC、聚合事件、写 outbox
- 没有"内嵌一个像 mcp_bridge 一样的子组件"的概念——下游 hermes 自己不读文件
- daemon 崩了消息流转立刻断

### 选哪种由"下游 agent 怎么收消息"决定，不由 agent-bridge 喜好决定

| 下游 agent 提供的接口 | daemon 站位 | 例子 |
|---|---|---|
| 在自己进程内可以 host 我们的代码（MCP server / 插件 / SDK） | 路径外 | claude_code（mcp_bridge 跑在 claude 进程内） |
| 只暴露外部 RPC / API / stdio | 路径内 | hermes_acp（ACP 是 stdio JSON-RPC，agent-bridge 必须在外面持有 socket） |

不要为了"统一"而强行让两个 backend 走同一路径。下游协议是什么样、daemon 就长什么样。

## 为什么不走"统一 daemon 模型"（B 方案）

讨论过一个方案：让 daemon 统一变成消息泵，所有 backend 都走
`router → daemon → BackendDriver.send/wait_for_reply → outbox`。**实测下来这是
过度抽象**，没有解锁任何具体能力：

| 假想优势 | 真实情况 |
|---|---|
| 可观察性集中在 daemon | 应该埋在 channel + router（含 inbound→outbound 全链路时间戳），跟 backend 内部架构无关 |
| 撤回/中断统一接口 | A 也能加（`inbox.cancel` 文件 + 各 backend 自己实现 cancel handler），代码量相当 |
| 流式回复 | 瓶颈是 outbox 协议升级（覆写 → jsonl tail），跟 daemon 站位无关 |
| 多用户路由 | 这是 router 层的事（peer_id → alias 映射），跟 backend 内部无关 |
| 统一 usage 上报 | 写一个 `usage.jsonl` 就够，两个 backend 各往里写 |
| daemon 是路径唯一节点 | 审美，没解锁能力。代价是 claude_code 链路被迫拆 mcp_bridge，把"消息能力 = child claude 活着"这个天然不变量打破 |

B 方案唯一真实价值：**daemon 做横向决策**（基于历史限流、跨 session 协调、缓存、去重、重试）。
但这些事根本不是 backend daemon 的视野，是 router / core 的事 —— daemon 只看一个 alias 一个下游 agent，没有跨 session 视角，也没必要有。

**结论**：A 方案（各 backend 选自己的 daemon 站位）是各司其职，B 方案是用复杂度换审美统一感。走 A。

## daemon 职责拆分

观察 `backends/claude_code/daemon.py` 现有职责，能区分出**通用**和**backend 专属**两类：

### 通用（抽到 `core/daemon_lifecycle.py`）

- CLI 解析 `<alias> <cwd>`，alias 缺省时用 `make_alias_for_cwd(cwd)` 生成
- alias 校验 + `chat_sessions/<alias>/` 目录创建
- 决定 spawn cwd：CLI > meta.json 历史 > backend 默认 > $HOME 兜底
- 日志：`daemon.log` via `logging.basicConfig`
- `meta.json` 原子写：alias / cwd / daemon_pid / child_pid / created_at / backend / 自定义 extra
- `spawned_pids.jsonl` 追加（含 psutil create_time 防 PID 复用）
- `atexit` + `SIGINT` 清理：调 backend 的 on_exit 回调，最后改 meta 写 last_exit_at + 两个 pid 清 null

### Backend 专属（留在 `backends/<name>/daemon.py`）

- 怎么 spawn 下游进程（claude_code：pywinpty + `claude.exe`；hermes_acp：subprocess + `hermes acp`）
- 怎么知道下游 ready（claude_code：等 TUI 欢迎屏 marker；hermes_acp：initialize / new_session response）
- 怎么处理下游异常（claude_code：trust-folder 对话框、rate-limit 撞墙后 press "3"；hermes_acp：session 失效后 reinit）
- 消息流转（如果 daemon 在消息路径上才做）

把这两类分开之后，新 backend 的 daemon 就是 ~50-100 行胶水加上必要的下游适配，
而不是从零抄一遍 ~330 行的 claude_code/daemon.py。

## 跨 backend 能力归属表

下面几个能力将来都可能要做，先把"该在哪一层做"钉死，避免下次又在 daemon 抽象上纠结：

| 能力 | 该在哪一层做 | 为什么 |
|---|---|---|
| 撤回 / 中断 turn | backend 内（各 daemon 实现 cancel handler，约定 `inbox.cancel` 文件触发） | 撤回必须能调下游具体的中断 API，跨 backend 的下游协议不一样 |
| 流式回复 | outbox 协议升级（覆写 → `outbox.jsonl` tail）+ 两个写端各支持 | 这是文件协议本身的演进，跟 daemon 站位无关 |
| 多用户路由（peer_id → alias） | router | router 是入口，是唯一拥有"哪个 peer 该映射到哪个 alias"全局视角的地方 |
| 限流（按 alias 历史 / 跨 session） | router | router 在消息进系统的第一道闸，有 history.json 全视角 |
| 去重 | router | 同上 |
| 重试 | 看重试什么。下游 API 调用失败 → backend 自己重试；消息投递失败 → channel 重试 | 跨边界的重试不应该跨层 |
| 缓存 | router 或 core | 基于 inbox 内容做命中判断，daemon 拿到时已经晚了一层 |
| Usage 上报 | backend 写 `usage.jsonl`，core 聚合 | 数据源在 backend，聚合视角在 core |
| 跨 session 协调 | core | daemon 没有跨 session 视角，强行让它有就是 B 方案的坑 |

**判断准则**：决策需要的视角在哪一层，就在哪一层做。daemon 视角 = 一个 alias 一个下游 agent。
凡是需要多 session 视角、需要 history 视角、需要 peer 视角的，都不该归 daemon。

## 添加新 backend 的清单

详细步骤参见 `docs/ADD_BACKEND.md`。这里只列结构决策：

1. 看下游 agent 的协议形态 → 决定 daemon 站位（路径外 / 路径内）
2. `backends/<name>/daemon.py`：用 `core/daemon_lifecycle` 起骨架，加下游适配
3. （路径外）一般还要 `<name>/bridge.py` 或类似——下游进程内运行、IO 文件
4. （路径内）daemon 内部加 inbox watcher + 下游协议 client + outbox writer
5. `meta.json` 写 `backend: "<name>"`
6. `core/spawn.py` 已经按 `meta.backend` 字段选 daemon 模块（默认 `claude_code` 向后兼容）
7. 创建会话时显式指定 backend（CLI / web /new 入口要带这个参数）

## 当前两个 backend 对比

| 维度 | claude_code | hermes_acp |
|---|---|---|
| 下游 agent 协议 | claude TUI + 自加载 MCP server | ACP stdio JSON-RPC |
| daemon 站位 | 路径外（看护者） | 路径内（中转者） |
| 进程拓扑 | daemon → claude.exe → mcp_bridge.py | daemon → hermes acp |
| 启动开销 | ~10s（TUI 加载 + 触发 chats-loop） | 首次 ~60-80s（LexAI client + vision probe），后续 ~1s |
| 消息流转 | mcp_bridge poll inbox + write outbox | daemon poll inbox + write outbox |
| 工具调用 | child claude 自决 | hermes 自决（带 terminal/code/web tools） |
| 多模态输入 | 不支持（PTY 没法粘图） | 原生 ImageContentBlock |
| 失败域 | daemon 死了消息不断（mcp_bridge 还活着）；child claude 死了才断 | daemon 死了消息立刻断 |
| 跨 session 隔离 | 进程级（最强） | sessionId 级（hermes 内部状态） |

## 不要做的事

- 不要把"通用 daemon 模型"再翻出来——已经讨论过两遍，不解锁任何能力（B 方案）
- 不要给 daemon 加跨 session 视角的功能——那是 core/router 的事
- 不要为了"backend 形态对称"硬改 claude_code 链路——mcp_bridge 现状是经过实际场景打磨的，不要为了审美回归风险
- 不要在 backend 层做用户路由——router 一处够了

## 默认 backend 契约（命令行入口用）

命令行入口（微信 `/proj`、`/new`）建会话时不让用户在每条消息里选 backend——
手机微信塞个选择 UI 体验会乱。改用一个**粘性的"默认 backend"**：

- 文件：`chat_sessions/_default_backend.txt`，内容是 backend 名（`claude_code` /
  `hermes_acp`），缺省（文件不存在或值不在 `KNOWN_BACKENDS` 里）按 `claude_code`。
- 读：`core.sessions.get_default_backend()`。
- 写：`core.sessions.set_default_backend(name)` 或 `/backend <name>` 命令。
- 作用域：只影响**之后**通过 `/proj N` / `/proj 0` 新建的会话；已有会话不变。
- web dashboard 建会话走自己的 backend 参数（modal 下拉），不读这个文件——
  浏览器有空间显式选，命令行没有。

### 已知 backend 集合

`core.sessions.KNOWN_BACKENDS` 是单一来源（目前 `("claude_code", "hermes_acp")`）。
新加 backend 时同步：
1. 这里加一项
2. `core.spawn._BACKEND_DAEMON_MODULES` 注册一条
3. `web.spawn_helpers._KNOWN_BACKENDS` 同步（dashboard 校验用）

为什么不让 `core.sessions` 反查 spawn 的注册表：sessions 是底层，spawn 依赖
sessions（加载 meta），方向反过来会成环。两处同步成本可接受，未来若变成 3+ 个
backend 再考虑抽到 paths.py 之类的更底层。

## 历史决策记录

- **2026-06-10**：决定 hermes_acp 走"1 alias = 1 daemon = 1 hermes acp 子进程"，
  跟 claude_code 同构。ACP 协议本身支持"单进程多 session"（让 N 个 alias 共享一个
  hermes acp 子进程，省启动开销），但落地到 agent-bridge 不用这个。理由：
  - 现有 router / spawn / meta 一套都是按 1 alias = 1 daemon 建模的，单进程模型要
    引入"backend 进程池"这个新概念，改动面大
  - 启动开销在单机 1-2 alias 的场景里不构成瓶颈
  - 失败域隔离更强：一个 alias 的 hermes 崩了不影响其它 alias
  - 跟 claude_code 同构降低心智负担
- **2026-06-10**：daemon 抽出 `core/daemon_lifecycle.py` 通用工具，但**不**统一消息泵模型（B 方案否决）。
- **2026-06-11**：命令行入口（微信 `/proj` / `/new`）建会话时不在每条消息里
  问 backend，改走"默认 backend 文件 + `/backend` 元命令"模式。理由：
  - 手机微信里塞个选择步骤体验差（多一次对话往返）
  - 用户切 backend 是低频动作，一次设了之后通常会连续用一段时间
  - 复用现有命令面，0 新增 UI 概念
  - dashboard 有空间显式选，所以浏览器路径不走这个文件，避免两条路径互相干扰
- **2026-06-11**：`list_sessions` 加 lazy-fix——扫到 meta 字面在线但 daemon
  PID 已不活的会话时，回写 meta 把 daemon_pid/child_pid 清成 null + 写
  `last_exit_at: "(detected_dead)"`。理由：`taskkill /F` / OOM / 解释器崩等
  情况会绕过 daemon 的 atexit 钩子，meta 字面留死 PID 误导 dashboard /
  `/list`。**不**在 `load_meta_for` 里做，避免给底层 read 路径加副作用——
  只在"全表扫描"路径（list_sessions）顺带做。PID 复用风险未处理（todo：
  init_lifecycle 时写 daemon_create_time，比对再判活）。
- **2026-06-11**：web 端口移到 `config.json:web_port`（缺省 8765）。理由：
  本机被别的项目占了 8765，必须能改。代码里所有"非例外位"全部走
  `core.config.get_web_port()` 单一来源。**例外**：hook 副本不能动态读
  config——它装在 `~/.claude/hooks/` 跑在 child claude 的环境，没法
  import 项目包。改用 install 时渲染——源文件里加 `# CHATS_BRIDGE_WEB_PORT_LINE`
  标记行，install.py 用 str.replace 注入当前值。所以改端口要重跑
  `install/install.py --hook`。同时删了过期的 `scripts/restart_all.ps1`
  （路径名还指向 claude-mcp-bridge 老仓库，调用的 `claude_daemon.py` 都
  不存在了），换成 `scripts/start_web_detached.py` + `scripts/stop_web.py`。
