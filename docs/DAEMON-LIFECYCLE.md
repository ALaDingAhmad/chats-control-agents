# claude_code daemon 生命周期

daemon 怎么把会话拉起来、怎么 drain child claude 的 PTY、怎么扛 rate
limit。动 `backends/claude_code/daemon.py` 之前必读——阶段顺序是有讲究
的，失败模式很隐蔽。

## 进程树

```
web_server.py (Starlette, port=config.json:web_port，缺省 :8765)
  └─ subprocess（detached）: daemon.py 每个 alias 一个
       └─ PtyProcess: claude.exe (child claude, cwd = 会话的 cwd)
            └─ subprocess（stdio）: mcp_bridge.py（env CHATS_LOOP_ALIAS=<alias>）
```

`spawn_daemon_detached`（在 `core.spawn` 里）Windows 用
`CREATE_NEW_PROCESS_GROUP | DETACHED | CREATE_NO_WINDOW`——daemon 能扛过
web_server 重启。daemon 被 SIGINT 或挂掉，`atexit` 会杀 child claude；child
claude 被外部杀，daemon 的 drain 循环会通过 `Pty is closed` 退出。

## Phase 1 — 等 TUI ready

daemon spawn `claude.exe` 后从 PTY 循环读，直到看到能证明 TUI 已经进入
主聊天屏的标记。

```
READY_MARKERS = ["Welcomeback", "Tipsforgetting"]
READY_TIMEOUT = 30  # 秒
```

**硬规则**：`READY_MARKERS` 里的每一项只能在 post-init 主屏出现，**不能**
在任何 pre-init 对话框里出现。早先这个列表含通用 box-drawing/prompt 字符
（`│ > ❯`）——这些在 trust-folder 对话框就匹配了，导致 trigger 被打字进
那个对话框，chats-loop 永远没激活。不要随便加进来。

### Trust-folder 对话框（卡住 Phase 1）

新 cwd 第一次启动，child claude 会卡在这个对话框：

```
Do you trust this folder?
❯ 1. Yes, I trust this folder
  2. No, exit
Enter to confirm · Esc to cancel
```

stdin 没东西写进去之前 `proc.read()` 永远阻塞，30 秒 `READY_TIMEOUT` 也
查不到（连 `time.time()` 都跑不到）。所以 daemon 必须在 Phase 1 读循环
里主动识别这个对话框并按 Enter。

**检测契约 — 必须 ANSI-blind。** child 是个 Ink TUI：交织着 SGR 颜色转义
（`\x1b[38;…m`）*和*用 cursor-right 转义（`\x1b[1C`）替代单词间的空格。
朴素 substring 永远命不中：raw buffer 里实际是
`"…\x1b[…mYes,\x1b[1CI\x1b[1Ctrust\x1b[1Cthis\x1b[1Cfolder\x1b[…m"`。

策略：substring 扫描前**剥掉所有 CSI 转义**（`\x1b[…<final>`）。cursor-right
不插空格，所以相邻单词会粘到一起：`"Yes,Itrustthisfolder"`。检测就必须用
**连写形式**的关键词：

```python
import re
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
def _ansi_blind(s: str) -> str:
    return _CSI.sub("", s)

if "trustthisfolder" in _ansi_blind(buffer):    # 不是 "trust this folder"
    proc.write("\r")     # 默认选中"1. Yes"
    trust_dismissed = True
    buffer = ""          # 清掉 buffer 防止后续片段误触
```

按 `\r` 而不是 `1\r`：默认 `❯` 已经在选项 1 上，多打个 `1` 万一焦点跑了
反而生成杂字符。

同样的 `_ansi_blind` + 连写形式规则也适用于 `READY_MARKERS`（
`"Welcomeback"`，不是 `"Welcome back"`）。

## Phase 2 — 发 trigger

Phase 1 确认 ready 之后，sleep 1.5 秒（让输入焦点稳定），然后写 trigger：

```
TRIGGER_COMMAND = "/chats-loop"
proc.write(TRIGGER_COMMAND + "\r")
```

这会触发用户 Claude Code 配置里注册的 chats-loop skill；skill 调
`mcp_bridge.relay_init`，进入
`wait_for_message → reply → send_chat_response → wait_for_message` 循环。

## Phase 3 — 确认激活（best effort）

读 PTY 输出最多 `POST_TRIGGER_SETTLE * 3 = 18` 秒，找
`"chats-loop loop active"`（或就 `"loop active"`）。best effort：这个 marker
在不同 Claude Code TUI 版本上不稳，所以 daemon 没看到也只是 WARN 一下继续，
不会中断。**发出 trigger 才是关键**——skill 是异步激活的，
`mcp_bridge.wait_for_message` 一旦 loop 跑起来就会开始轮询 `inbox.txt`，
跟有没有打印那段文字无关。

## Phase 4 — drain 循环 + rate-limit 看门狗

```
while proc.isalive():
    chunk = proc.read(4096)
    pty_log.write(chunk)
    if RATE_LIMIT_MARKERS in pty_buffer:
        press "3\r"            # "Stop and wait"
        write outbox notice    # 通过渠道告知用户
        rate_limited = True
    if rate_limited and "loop active" in pty_buffer:
        write "Claude 已恢复"
        rate_limited = False
    if rate_limited and now - last_trigger_retry >= 300:
        proc.write(TRIGGER + "\r")
```

**rate-limit 弹窗处理。** API 配额耗尽时 TUI 弹 `You've hit your limit` +
`/rate-limit-options` 1/2/3。daemon 选 3（`Stop and wait`），往
`outbox.txt` 写一条通知（**绕开 child claude**——它在 rate-limit 状态下没法
回复），然后每 5 分钟重发 trigger。限额重置后下一次 trigger 能进 skill，
skill 打出 `"loop active"`，daemon 清掉 rate_limited 标记。

**`3\r` cooldown**：连续两次按 3 之间至少 60 秒，避免瞬间重渲染导致连按。

## 加新弹窗处理器的步骤

child claude 可能弹出任何 modal（工具调用授权、操作确认、登录失效）都
按这套来：

1. 决定放哪个 phase（trigger 前 = Phase 1；trigger 后持续 = Phase 4）。
2. 用 `daemon.py` 里定义的 `_ansi_blind`。它会剥掉所有 CSI 转义（cursor-move
   *和* SGR 颜色）。然后关键词写**连写形式**——cursor-right 不插空格，所以
   单词会粘到一起（`"Yes,Itrustthisfolder"`，不是 `"Yes, I trust this folder"`）。
3. 写应答 keystroke。能用 `\r` 就用 `\r`（接受默认）；非要选具体编号才写
   数字。
4. 一次性匹配命中后 `buffer = ""` 防止后续误触。
5. log 一下（`log.info("xxx 弹窗：按了 N")`）——静默处理弹窗是最难 debug
   的 bug 类型。

当前已处理：trust-folder（Phase 1）、rate-limit（Phase 4）。
**已知未处理**：工具调用授权、操作确认弹窗。

## detached PTY 调试小贴士

child claude 没有控制台窗口，没法 attach 看屏幕。可信信息源按有用程度
排序：

- `chat_sessions/<alias>/daemon.log` — 高层 phase 转换 + 警告/错误。
- `chat_sessions/<alias>/pty.log` — 含 ANSI 的 PTY 原始字节。**不要 `cat`**，
  动辄 100MB+。要用 `Read offset=…` 或先剥 ANSI 再 print。
- 项目根 `mcp_bridge.log` — 告诉你 `wait_for_message` 有没有被调用过。
  没有 = chats-loop skill 没激活 = 回头看 Phase 2 / Phase 3。

## marker 文件

`~/.claude/.chats-loop-active-<alias>` 在 mcp_bridge 第一次 `wait_for_message`
调用时 touch，atexit 时删。它存在 = skill 在那个 alias 下激活过；
spawn 看起来正常但文件不存在 = skill 没跑，几乎肯定是 Phase 2 把 trigger
打字到了错位置。
