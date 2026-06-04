"""
Web chat server for phone-to-Claude bridge POC.

Architecture:
  Browser → POST /send → write phone_inbox.txt
                      ← (user goes to Claude Code window, says "查手机消息")
                         → Claude reads inbox via MCP, generates reply, writes outbox
  Browser ← GET /poll  ← read phone_outbox.txt (when new content appears)

POC limits:
  - User must manually trigger Claude in the Claude Code window after sending.
  - Single-message inbox/outbox (no queue). Send one, wait for reply, then send next.
  - Bind 127.0.0.1 only. No auth.

Run:
  python D:/aiproject/claude-mcp-bridge/web_server.py
Then open: http://127.0.0.1:8765/
"""
import asyncio
import json
import os
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

import weixin_protocol as wx
import weixin_state as wxs
import sessions as sx

ROOT = Path(__file__).parent
LOG_PATH = ROOT / "web_server.log"

# Run legacy migration once at import time
sx.migrate_legacy_if_present()


# Per-alias IO accessors — replace the old global INBOX/OUTBOX/HISTORY.
def inbox_for(alias: str) -> Path:
    return sx.inbox_path(alias)


def outbox_for(alias: str) -> Path:
    return sx.outbox_path(alias)


def history_for(alias: str) -> Path:
    return sx.history_path(alias)


# Per-alias outbox-watcher state. Keys are alias strings; value is fp of the
# last reply we already forwarded to WeChat for that alias.
_outbox_seen: dict[str, str] = {}

# In-memory WeChat session state. Populated by /weixin/qr/start and
# /weixin/qr/status during login; mutated by the background tasks.
_wx: dict = {
    "qrcode": None,                # hex token from get_qrcode
    "qrcode_img_content": None,    # full URL to encode as QR
    "qr_session_base_url": wx.ILINK_BASE_URL,  # may redirect mid-login
    "qr_status": "idle",           # idle / waiting / scaned / confirmed / expired / error
    "qr_error": None,
    "qr_started_at": None,
    "last_peer_id": None,          # most recent inbound sender (for routing replies)
    "tasks": [],                   # background asyncio.Task list
    "outbox_last_pushed": "",      # fingerprint of last reply we forwarded
    "running": False,              # whether the long-poll loop is alive
}

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("web")


def _load_history(alias: str | None = None):
    if alias is None:
        alias = sx.get_current()
    p = history_for(alias)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_history(items, alias: str | None = None):
    if alias is None:
        alias = sx.get_current()
    p = history_for(alias)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


HTML_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude 桥接</title>
<style>
  body { font-family: -apple-system, "Segoe UI", sans-serif; margin: 0; background: #f5f5f5; }
  #app { max-width: 720px; margin: 0 auto; padding: 12px; }
  #log { background: #fff; border: 1px solid #ddd; border-radius: 6px;
         height: 60vh; overflow-y: auto; padding: 12px; }
  .msg { margin: 8px 0; padding: 8px 12px; border-radius: 8px; max-width: 80%;
         white-space: pre-wrap; word-break: break-word; }
  .user { background: #d0e4ff; margin-left: auto; text-align: left; }
  .claude { background: #efefef; }
  .meta { font-size: 11px; color: #999; margin-bottom: 2px; }
  .row { display: flex; }
  .row.user { justify-content: flex-end; }
  #form { display: flex; gap: 8px; margin-top: 10px; }
  #input { flex: 1; padding: 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
  #send { padding: 10px 20px; background: #007aff; color: #fff; border: 0;
          border-radius: 6px; cursor: pointer; font-size: 14px; }
  #send:disabled { background: #999; }
  #status { font-size: 12px; color: #888; margin-top: 6px; min-height: 16px; }
  /* Workspace + projects panel */
  details.panel { background: #fff; border: 1px solid #ddd; border-radius: 6px;
                  margin-bottom: 8px; padding: 6px 12px; }
  details.panel > summary { cursor: pointer; font-size: 13px; color: #555;
                            padding: 4px 0; user-select: none; }
  details.panel[open] > summary { color: #007aff; font-weight: 500; }
  .ws-list { margin: 8px 0 4px 0; }
  .ws-row { display: flex; gap: 6px; align-items: center; margin: 4px 0; font-size: 13px; }
  .ws-row input { flex: 1; padding: 5px 8px; border: 1px solid #ccc;
                  border-radius: 4px; font-size: 13px; font-family: monospace; }
  .ws-row input.missing { border-color: #d33; background: #fee; }
  .ws-row button { padding: 4px 10px; border: 0; border-radius: 4px;
                   cursor: pointer; font-size: 12px; }
  .ws-row button.del { background: #fdd; color: #a00; }
  .ws-actions { display: flex; gap: 6px; margin-top: 6px; }
  .ws-actions button { padding: 6px 14px; border: 0; border-radius: 4px;
                       cursor: pointer; font-size: 13px; }
  .ws-actions button.primary { background: #007aff; color: #fff; }
  .ws-actions button.add { background: #e5f1ff; color: #007aff; }
  .ws-msg { font-size: 12px; color: #888; min-height: 16px; margin-top: 4px; }
  .ws-msg.err { color: #d33; }
  .ws-msg.ok { color: #2a8; }
  /* Projects list */
  .proj-current { font-size: 12px; color: #666; margin: 8px 0 4px 0; }
  .proj-list { max-height: 200px; overflow-y: auto; border: 1px solid #eee;
               border-radius: 4px; }
  .proj-item { display: flex; gap: 8px; padding: 6px 8px; align-items: center;
               border-bottom: 1px solid #f0f0f0; font-size: 13px; cursor: pointer; }
  .proj-item:last-child { border-bottom: 0; }
  .proj-item:hover { background: #f7f9ff; }
  .proj-item.online { font-weight: 500; }
  .proj-item .name { flex: 1; }
  .proj-item .tag { font-size: 11px; padding: 1px 6px; border-radius: 3px; }
  .proj-item .tag.online { background: #def0d0; color: #2a8; }
  .proj-item .tag.offline { background: #ffe8c0; color: #a60; }
  .proj-item .tag.idle { background: #eee; color: #888; }
  .proj-item .root { font-size: 11px; color: #aaa; font-family: monospace; }
</style>
</head>
<body>
<div id="app">
  <details class="panel" id="ws-panel">
    <summary>⚙️ 工作空间与项目（点击展开）</summary>
    <div style="margin-top: 6px;">
      <div style="font-size: 12px; color: #888;">配置工作空间根目录，/proj 命令会扫描这些目录下的子目录。</div>
      <div class="ws-list" id="ws-list"></div>
      <div class="ws-actions">
        <button type="button" class="add" id="ws-add">+ 添加目录</button>
        <button type="button" class="primary" id="ws-save">保存</button>
      </div>
      <div class="ws-msg" id="ws-msg"></div>

      <div class="proj-current" id="proj-current"></div>
      <div class="proj-list" id="proj-list"></div>
    </div>
  </details>
  <div id="log"></div>
  <form id="form">
    <input id="input" placeholder="输入消息，回车发送…" autocomplete="off">
    <button id="send" type="submit">发送</button>
  </form>
  <div id="status"></div>
</div>
<script>
const logEl = document.getElementById('log');
const formEl = document.getElementById('form');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');
let pollTimer = null;

function render(items) {
  logEl.innerHTML = '';
  for (const m of items) {
    const row = document.createElement('div');
    row.className = 'row ' + m.role;
    const div = document.createElement('div');
    div.className = 'msg ' + m.role;
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = (m.role === 'user' ? '我' : 'Claude') + ' · ' + m.ts.slice(11, 19);
    div.appendChild(meta);
    const body = document.createElement('div');
    body.textContent = m.text;
    div.appendChild(body);
    row.appendChild(div);
    logEl.appendChild(row);
  }
  logEl.scrollTop = logEl.scrollHeight;
}

async function fetchHistory() {
  const r = await fetch('/history');
  const items = await r.json();
  render(items);
  return items;
}

async function send(text) {
  sendBtn.disabled = true;
  statusEl.textContent = '已发送，等待 Claude 回复…（如果是第一条，先在 Claude Code 窗口说"启动 web-relay"）';
  await fetch('/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text}),
  });
  const items = await fetchHistory();
  pollSince = Array.isArray(items) ? items.length : 0;
  startPoll();
}

// Persistent poll: as long as the page is open we keep polling so new
// history entries land in the UI immediately. We don't try to "guess" when
// Claude is done — mcp calls can block arbitrarily long (user idle, Claude
// waiting on slow tools, etc.). The send button unlocks as soon as we see
// any new history entry after our last send, so the user can keep typing.
let pollSince = 0;       // marker: number of history items at time of send
function startPoll() {
  if (pollTimer) return;
  let lastLen = -1;
  pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/poll');
      const data = await r.json();
      if (data.history && data.history.length !== lastLen) {
        render(data.history);
        lastLen = data.history.length;
        // Unlock send button when at least one new entry has arrived since
        // this send. Mid-turn narration counts — user sees something is
        // happening and may want to type next question already.
        if (data.history.length > pollSince) {
          sendBtn.disabled = false;
          if (statusEl.textContent.startsWith('已发送')) statusEl.textContent = '';
        }
      }
    } catch (e) {
      // transient network blip — keep going
    }
  }, 1500);
}

formEl.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  send(text);
});

// ── Workspace + projects panel ─────────────────────────────────────────
const wsListEl = document.getElementById('ws-list');
const wsAddBtn = document.getElementById('ws-add');
const wsSaveBtn = document.getElementById('ws-save');
const wsMsgEl = document.getElementById('ws-msg');
const projCurrentEl = document.getElementById('proj-current');
const projListEl = document.getElementById('proj-list');
const wsPanel = document.getElementById('ws-panel');

let _wsExisting = new Set();  // lowercase paths that actually exist on disk

function renderWorkspaceEditor(roots) {
  wsListEl.innerHTML = '';
  for (const r of roots) {
    addWorkspaceRow(r);
  }
}

function addWorkspaceRow(value) {
  const row = document.createElement('div');
  row.className = 'ws-row';
  const input = document.createElement('input');
  input.type = 'text';
  input.value = value || '';
  input.placeholder = '例如 D:/aiproject 或 /home/user/projects';
  if (value && !_wsExisting.has(value.toLowerCase().replace(/\\\\/g, '/').replace(/\\/$/, ''))) {
    input.classList.add('missing');
    input.title = '该目录在磁盘上不存在';
  }
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'del';
  del.textContent = '删除';
  del.addEventListener('click', () => row.remove());
  row.appendChild(input);
  row.appendChild(del);
  wsListEl.appendChild(row);
  if (!value) input.focus();
}

wsAddBtn.addEventListener('click', () => addWorkspaceRow(''));

wsSaveBtn.addEventListener('click', async () => {
  const inputs = wsListEl.querySelectorAll('input');
  const roots = Array.from(inputs).map(i => i.value.trim()).filter(v => v);
  wsMsgEl.className = 'ws-msg';
  wsMsgEl.textContent = '保存中…';
  try {
    const r = await fetch('/config/workspace', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({workspace_roots: roots}),
    });
    const data = await r.json();
    if (!data.ok) {
      wsMsgEl.className = 'ws-msg err';
      wsMsgEl.textContent = '保存失败：' + (data.reason || '未知错误');
      return;
    }
    wsMsgEl.className = 'ws-msg ok';
    wsMsgEl.textContent = `已保存 ${data.workspace_roots.length} 个目录（其中 ${data.existing.length} 个有效）`;
    _wsExisting = new Set(data.existing.map(p => p.toLowerCase().replace(/\\\\/g, '/').replace(/\\/$/, '')));
    renderWorkspaceEditor(data.workspace_roots);
    fetchProjects();  // refresh project list with new roots
  } catch (e) {
    wsMsgEl.className = 'ws-msg err';
    wsMsgEl.textContent = '保存失败：' + e.message;
  }
});

async function fetchConfig() {
  try {
    const r = await fetch('/config');
    const data = await r.json();
    _wsExisting = new Set(data.existing.map(p => p.toLowerCase().replace(/\\\\/g, '/').replace(/\\/$/, '')));
    renderWorkspaceEditor(data.workspace_roots);
  } catch (e) {
    wsMsgEl.className = 'ws-msg err';
    wsMsgEl.textContent = '读取配置失败：' + e.message;
  }
}

async function fetchProjects() {
  try {
    const [pr, sr] = await Promise.all([
      fetch('/projects').then(r => r.json()),
      fetch('/sessions').then(r => r.json()),
    ]);
    const currentAlias = sr.current;
    projCurrentEl.textContent = `当前会话：${currentAlias}`;
    projListEl.innerHTML = '';
    pr.projects.forEach((p, i) => {
      const item = document.createElement('div');
      item.className = 'proj-item' + (p.online ? ' online' : '');
      const num = document.createElement('span');
      num.textContent = `${i + 1}.`;
      num.style.color = '#aaa';
      num.style.minWidth = '24px';
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = p.name;
      const tag = document.createElement('span');
      if (p.online) {
        tag.className = 'tag online';
        tag.textContent = `在线 → ${p.alias}`;
      } else if (p.alias) {
        tag.className = 'tag offline';
        tag.textContent = `离线 → ${p.alias}`;
      } else {
        tag.className = 'tag idle';
        tag.textContent = '未运行';
      }
      const root = document.createElement('span');
      root.className = 'root';
      root.textContent = p.root;
      item.appendChild(num);
      item.appendChild(name);
      item.appendChild(tag);
      item.appendChild(root);
      item.addEventListener('click', () => pickProject(i + 1, p));
      projListEl.appendChild(item);
    });
  } catch (e) {
    projCurrentEl.textContent = '加载项目失败：' + e.message;
  }
}

async function pickProject(n, p) {
  // Use the same /send -> /proj -> integer pick flow as WeChat so behavior is
  // identical across surfaces. First send /proj to populate the choice list,
  // then send the integer immediately.
  statusEl.textContent = `切换到 ${p.name}…`;
  try {
    await fetch('/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: '/proj'}),
    });
    const r = await fetch('/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: String(n)}),
    });
    const data = await r.json();
    if (data.reply) {
      statusEl.textContent = data.reply.split('\\n')[0].slice(0, 80);
    } else {
      statusEl.textContent = '已切换';
    }
    await fetchHistory();
    fetchProjects();  // refresh online/offline state
  } catch (e) {
    statusEl.textContent = '切换失败：' + e.message;
  }
}

// Refresh project state whenever the panel is opened
wsPanel.addEventListener('toggle', () => {
  if (wsPanel.open) {
    fetchConfig();
    fetchProjects();
  }
});

// Boot: render history once, then start permanent poll loop so messages
// from another tab / a mid-turn hook still appear without user interaction.
fetchHistory().then(items => {
  pollSince = Array.isArray(items) ? items.length : 0;
  startPoll();
});

// Eagerly fetch config + projects on boot so first panel open is instant.
fetchConfig();
fetchProjects();
</script>
</body>
</html>
"""


async def index(request):
    return HTMLResponse(HTML_PAGE)


async def get_history(request):
    # ?alias=X overrides current; defaults to current selection
    alias = request.query_params.get("alias") or sx.get_current()
    return JSONResponse(_load_history(alias))


async def list_sessions_route(request):
    return JSONResponse({"sessions": sx.list_sessions(), "current": sx.get_current()})


async def send_message(request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "reason": "empty"})

    # Slash commands handled by sessions module — never go to Claude.
    if sx.is_command(text):
        reply = sx.handle_command(text)
        # Append both the command and its response to the current session's
        # history so the user sees the exchange in the chat UI.
        alias = sx.get_current()
        history = _load_history(alias)
        ts = _now_iso()
        history.append({"role": "user", "text": text, "ts": ts, "source": "browser:command"})
        history.append({"role": "assistant", "text": reply, "ts": ts, "source": "command"})
        _save_history(history, alias)
        log.info("send: command %r", text[:50])
        return JSONResponse({"ok": True, "command": True, "reply": reply})

    # Regular message: route to currently selected session.
    # `//foo` is the passthrough escape — strip one slash so child claude sees /foo.
    text = sx.strip_passthrough_prefix(text)
    alias = sx.get_current()
    log.info("send to %s: %d chars", alias, len(text))
    # Clear stale outbox FIRST so /poll won't return last turn's reply as
    # if it were the answer to this new message.
    try:
        outbox_for(alias).write_text("", encoding="utf-8")
    except Exception as e:
        log.warning("clear outbox failed: %s", e)
    inbox_for(alias).parent.mkdir(parents=True, exist_ok=True)
    inbox_for(alias).write_text(text, encoding="utf-8")
    history = _load_history(alias)
    history.append({"role": "user", "text": text, "ts": _now_iso()})
    _save_history(history, alias)
    return JSONResponse({"ok": True, "alias": alias})


async def relay_push(request):
    """Receive a tap from the PreToolUse hook with assistant narration harvested
    from the transcript. The hook can pass `alias` directly (preferred) or we
    fall back to the current selection.
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "reason": "empty"})
    alias = body.get("alias") or sx.get_current()

    history = _load_history(alias)
    last_assistant = next(
        (m for m in reversed(history) if m["role"] == "assistant"), None
    )
    if last_assistant and last_assistant["text"].strip() in text:
        if last_assistant["text"].strip() == text:
            log.info("relay_push[%s]: dedup exact match", alias)
            return JSONResponse({"ok": True, "appended": False})
        extra = text.replace(last_assistant["text"].strip(), "").strip()
        if extra:
            text = extra
        else:
            return JSONResponse({"ok": True, "appended": False})

    history.append({
        "role": "assistant", "text": text, "ts": _now_iso(),
        "fp": "hook|" + text[:80],
        "source": body.get("source") or "hook",
    })
    _save_history(history, alias)
    log.info("relay_push[%s]: appended %d chars", alias, len(text))
    return JSONResponse({"ok": True, "appended": True, "alias": alias})


async def poll(request):
    """Check if outbox has a fresh reply. Returns {changed, history, alias}.

    Reads the outbox of the CURRENT selected session. Switching sessions in
    another tab automatically swings poll to the new session's outbox.
    """
    alias = request.query_params.get("alias") or sx.get_current()
    p = outbox_for(alias)
    if not p.exists():
        return JSONResponse({"changed": False, "history": _load_history(alias), "alias": alias})
    content = p.read_text(encoding="utf-8").strip()
    if not content:
        return JSONResponse({"changed": False, "history": _load_history(alias), "alias": alias})

    # outbox format from mcp_bridge.py: "[HH:MM:SS]\n<reply>"
    lines = content.split("\n", 1)
    stamp = lines[0] if content.startswith("[") else ""
    reply = lines[1] if len(lines) > 1 else content

    history = _load_history(alias)
    last_assistant = next(
        (m for m in reversed(history) if m["role"] == "assistant"), None
    )
    fingerprint = stamp + "|" + reply
    if last_assistant and last_assistant.get("fp") == fingerprint:
        return JSONResponse({"changed": False, "history": history, "alias": alias})

    history.append({
        "role": "assistant", "text": reply, "ts": _now_iso(), "fp": fingerprint,
    })
    _save_history(history, alias)
    log.info("poll[%s]: new reply %d chars", alias, len(reply))
    return JSONResponse({"changed": True, "history": history, "alias": alias})


WEIXIN_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>微信桥接</title>
<style>
  body { font-family: -apple-system, "Segoe UI", sans-serif; background: #f5f5f5; margin: 0; }
  #app { max-width: 480px; margin: 24px auto; background: #fff; padding: 24px;
         border-radius: 8px; border: 1px solid #ddd; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  .status { padding: 12px; background: #efefef; border-radius: 6px;
            font-size: 14px; margin-bottom: 16px; }
  .status.connected { background: #d4edda; color: #155724; }
  .status.error { background: #f8d7da; color: #721c24; }
  button { padding: 10px 18px; background: #07c160; color: #fff; border: 0;
           border-radius: 6px; cursor: pointer; font-size: 14px; }
  button:disabled { background: #999; }
  button.secondary { background: #6c757d; }
  #qr-container { text-align: center; margin: 20px 0; min-height: 240px; }
  #qr-container canvas, #qr-container img { display: inline-block; }
  .hint { color: #666; font-size: 12px; margin-top: 6px; }
  .meta { font-size: 12px; color: #888; word-break: break-all; }
</style>
</head>
<body>
<div id="app">
  <h1>微信桥接</h1>
  <div id="status" class="status">检查状态中...</div>
  <div>
    <button id="btn-start">开始扫码登录</button>
    <button id="btn-stop" class="secondary" style="display:none">断开账号</button>
  </div>
  <div id="qr-container"></div>
  <div class="hint" id="hint"></div>
  <div class="meta" id="meta"></div>
  <p style="margin-top:24px; font-size:12px; color:#666;">
    扫码登录后，对绑定的微信账号说话，消息会被转发到 Claude；Claude 的回复也会发回微信。
    <br>聊天页面：<a href="/">/</a>
  </p>
</div>
<!-- qrcode.js — encodes a URL string into a scannable QR canvas/img client-side -->
<script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
<script>
const statusEl = document.getElementById('status');
const btnStart = document.getElementById('btn-start');
const btnStop  = document.getElementById('btn-stop');
const qrEl     = document.getElementById('qr-container');
const hintEl   = document.getElementById('hint');
const metaEl   = document.getElementById('meta');
let qrObj = null;
let pollTimer = null;

function setStatus(text, cls) {
  statusEl.textContent = text;
  statusEl.className = 'status' + (cls ? ' ' + cls : '');
}

function renderQR(url) {
  qrEl.innerHTML = '';
  qrObj = new QRCode(qrEl, { text: url, width: 240, height: 240,
                              correctLevel: QRCode.CorrectLevel.M });
}

async function refresh() {
  const r = await fetch('/weixin/status');
  const d = await r.json();
  if (d.connected) {
    setStatus(`已连接（${d.account_id || '未知账号'}）`, 'connected');
    btnStart.style.display = 'none';
    btnStop.style.display = 'inline-block';
    qrEl.innerHTML = '';
    hintEl.textContent = `长轮询: ${d.running ? '运行中' : '未运行'}`;
    metaEl.textContent = '';
  } else {
    setStatus('未连接');
    btnStart.style.display = 'inline-block';
    btnStop.style.display = 'none';
    if (d.qr_status === 'waiting' && d.qrcode_img_content) {
      renderQR(d.qrcode_img_content);
      hintEl.textContent = '请用要绑定的微信扫描以上二维码并同意授权';
      metaEl.textContent = '原始链接：' + d.qrcode_img_content;
    } else if (d.qr_status === 'scaned') {
      hintEl.textContent = '已扫码，请在微信里确认...';
    } else if (d.qr_status === 'expired') {
      hintEl.textContent = '二维码已过期，请重新点击"开始扫码"';
      qrEl.innerHTML = '';
    } else if (d.qr_status === 'error') {
      setStatus('登录错误：' + (d.qr_error || '未知'), 'error');
    } else {
      qrEl.innerHTML = '';
      hintEl.textContent = '';
      metaEl.textContent = '';
    }
  }
}

async function start() {
  btnStart.disabled = true;
  setStatus('请求二维码...');
  try {
    const r = await fetch('/weixin/qr/start', {method: 'POST'});
    const d = await r.json();
    if (!d.ok) {
      setStatus('请求失败：' + (d.error || '未知'), 'error');
      btnStart.disabled = false;
      return;
    }
  } catch (e) {
    setStatus('网络错误：' + e.message, 'error');
    btnStart.disabled = false;
    return;
  }
  btnStart.disabled = false;
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refresh, 1500);
  await refresh();
}

async function stop() {
  if (!confirm('确定断开微信？')) return;
  await fetch('/weixin/disconnect', {method: 'POST'});
  await refresh();
}

btnStart.addEventListener('click', start);
btnStop.addEventListener('click', stop);
refresh();
// Keep refreshing slowly so a connected session reflects long-poll status
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


async def weixin_page(request):
    return HTMLResponse(WEIXIN_PAGE)


async def weixin_status(request):
    acct = wxs.load_account()
    connected = bool(acct and acct.get("bot_token"))
    return JSONResponse({
        "connected": connected,
        "account_id": (acct or {}).get("ilink_bot_id"),
        "running": _wx.get("running", False),
        "qr_status": _wx.get("qr_status"),
        "qr_error": _wx.get("qr_error"),
        "qrcode_img_content": _wx.get("qrcode_img_content"),
        "last_peer_id": _wx.get("last_peer_id"),
    })


async def weixin_qr_start(request):
    """Kick off the QR login flow. Returns immediately after fetching the
    QR; the actual status-polling is handled by the background task we
    spawn here. Frontend polls /weixin/status to see progress.
    """
    # Cancel any prior login attempt
    _cancel_tasks_named("qr_login")
    _wx["qr_status"] = "starting"
    _wx["qr_error"] = None
    try:
        async with wx._build_session() as session:
            qr = await wx.get_qrcode(session)
            if not qr.get("qrcode"):
                _wx["qr_status"] = "error"
                _wx["qr_error"] = "上游未返回 qrcode"
                return JSONResponse({"ok": False, "error": _wx["qr_error"]})
            _wx["qrcode"] = qr["qrcode"]
            _wx["qrcode_img_content"] = qr["qrcode_img_content"]
            _wx["qr_session_base_url"] = wx.ILINK_BASE_URL
            _wx["qr_status"] = "waiting"
            _wx["qr_started_at"] = datetime.now().isoformat(timespec="seconds")
        t = asyncio.create_task(_qr_login_loop(), name="qr_login")
        _wx["tasks"].append(t)
        log.info("weixin qr started: %s", qr["qrcode"][:8])
        return JSONResponse({"ok": True, "qrcode_img_content": qr["qrcode_img_content"]})
    except Exception as e:
        log.exception("weixin qr_start failed: %s", e)
        _wx["qr_status"] = "error"
        _wx["qr_error"] = str(e)
        return JSONResponse({"ok": False, "error": str(e)})


async def weixin_disconnect(request):
    """Stop long-poll, clear stored account. User must scan again next time."""
    _cancel_tasks_named("longpoll", "outbox_watch", "qr_login")
    _wx["running"] = False
    _wx["qr_status"] = "idle"
    _wx["qrcode"] = None
    _wx["qrcode_img_content"] = None
    wxs.clear_account()
    log.info("weixin disconnected, credentials cleared")
    return JSONResponse({"ok": True})


def _cancel_tasks_named(*names: str) -> None:
    keep = []
    for t in _wx.get("tasks", []):
        if t.get_name() in names and not t.done():
            t.cancel()
        else:
            keep.append(t)
    _wx["tasks"] = keep


async def _qr_login_loop():
    """Poll iLink for QR status until confirmed / expired / cancelled.
    On confirmed, save credentials and spawn long-poll + outbox watcher.
    """
    try:
        deadline_secs = 480  # ~8 minutes total
        start = asyncio.get_event_loop().time()
        async with wx._build_session() as session:
            while asyncio.get_event_loop().time() - start < deadline_secs:
                qrcode = _wx.get("qrcode")
                if not qrcode:
                    return
                base = _wx.get("qr_session_base_url", wx.ILINK_BASE_URL)
                try:
                    resp = await wx.poll_qrcode_status(
                        session, qrcode=qrcode, base_url=base,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("qr poll error: %s", e)
                    await asyncio.sleep(1)
                    continue

                status = str(resp.get("status") or "wait")
                if status == "wait":
                    _wx["qr_status"] = "waiting"
                elif status == "scaned":
                    _wx["qr_status"] = "scaned"
                elif status == "scaned_but_redirect":
                    redirect_host = str(resp.get("redirect_host") or "")
                    if redirect_host:
                        _wx["qr_session_base_url"] = f"https://{redirect_host}"
                        log.info("qr redirected to %s", redirect_host)
                elif status == "expired":
                    _wx["qr_status"] = "expired"
                    log.info("qr expired")
                    return
                elif status == "confirmed":
                    account = {
                        "ilink_bot_id": str(resp.get("ilink_bot_id") or ""),
                        "bot_token": str(resp.get("bot_token") or ""),
                        "baseurl": str(resp.get("baseurl") or wx.ILINK_BASE_URL),
                        "ilink_user_id": str(resp.get("ilink_user_id") or ""),
                    }
                    if not account["ilink_bot_id"] or not account["bot_token"]:
                        _wx["qr_status"] = "error"
                        _wx["qr_error"] = "上游 confirmed 但凭证字段缺失"
                        return
                    wxs.save_account(account)
                    _wx["qr_status"] = "confirmed"
                    _wx["qrcode"] = None
                    log.info("weixin connected as %s", account["ilink_bot_id"])
                    # Kick off runtime tasks
                    _start_runtime_tasks(account)
                    return
                await asyncio.sleep(1)
        # Timed out
        _wx["qr_status"] = "expired"
    except asyncio.CancelledError:
        log.info("qr_login_loop cancelled")
        raise
    except Exception as e:
        log.exception("qr_login_loop crashed: %s", e)
        _wx["qr_status"] = "error"
        _wx["qr_error"] = str(e)


def _start_runtime_tasks(account: dict) -> None:
    """Spawn long-poll (inbound) and outbox-watcher (outbound) tasks."""
    _cancel_tasks_named("longpoll", "outbox_watch")
    _wx["running"] = True
    t1 = asyncio.create_task(_inbound_longpoll(account), name="longpoll")
    t2 = asyncio.create_task(_outbox_watcher(account), name="outbox_watch")
    _wx["tasks"].extend([t1, t2])


async def _inbound_longpoll(account: dict):
    """Long-poll iLink getupdates, write inbound text into chat_inbox.txt.

    sync_buf advances each round so we don't re-receive the same messages.
    Records the latest peer's context_token so replies route correctly.
    """
    token = account["bot_token"]
    base_url = account["baseurl"]
    sync_buf = ""
    backoff = 2
    log.info("weixin longpoll starting, base=%s", base_url)
    try:
        async with wx._build_session() as session:
            while _wx.get("running"):
                try:
                    resp = await wx.get_updates(
                        session, base_url=base_url, token=token, sync_buf=sync_buf,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("longpoll error: %s, backoff=%ds", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                backoff = 2

                ret = resp.get("ret")
                if ret not in (0, None):
                    errmsg = resp.get("errmsg") or ""
                    log.warning("longpoll ret=%s errmsg=%s", ret, errmsg)
                    # Stale session — most likely needs re-login
                    if ret in (-14, -2):
                        log.warning("session stale, stopping longpoll")
                        _wx["running"] = False
                        return
                    await asyncio.sleep(2)
                    continue

                # Advance cursor
                sync_buf = str(resp.get("get_updates_buf") or sync_buf)

                msgs = resp.get("msgs") or []
                for msg in msgs:
                    parsed = wx.extract_text_and_meta(msg)
                    if not parsed:
                        continue
                    sender, text, ctx_tok = parsed
                    _wx["last_peer_id"] = sender
                    if ctx_tok:
                        wxs.set_context_token(sender, ctx_tok)
                    log.info("weixin inbound from=%s chars=%d", sender[:8], len(text))

                    # Slash commands handled in-process; reply directly to WeChat.
                    if sx.is_command(text):
                        reply = sx.handle_command(text)
                        try:
                            resp = await wx.send_text(
                                session, base_url=base_url, token=token,
                                to_user_id=sender, text=reply, context_token=ctx_tok,
                            )
                            ret = resp.get("ret")
                            if ret not in (0, None):
                                log.warning("weixin command %r reply ret=%s errmsg=%s",
                                            text[:30], ret, resp.get("errmsg"))
                        except Exception as e:
                            log.warning("weixin command reply failed: %s", e)
                        continue

                    # Regular message → route to currently selected session.
                    # `//foo` passthrough escape → strip one slash so child claude sees /foo.
                    text = sx.strip_passthrough_prefix(text)
                    alias = sx.get_current()
                    # Remember which alias this peer is currently sending into,
                    # so when that alias's outbox produces a reply we can echo
                    # it back to the right WeChat peer.
                    _wx.setdefault("alias_peer", {})[alias] = sender
                    inbox_for(alias).parent.mkdir(parents=True, exist_ok=True)
                    try:
                        inbox_for(alias).write_text(text, encoding="utf-8")
                    except Exception as e:
                        log.warning("inbox write failed: %s", e)
                    history = _load_history(alias)
                    history.append({
                        "role": "user", "text": text, "ts": _now_iso(),
                        "source": f"weixin:{sender[:8]}",
                    })
                    _save_history(history, alias)
    except asyncio.CancelledError:
        log.info("longpoll cancelled")
        raise
    except Exception as e:
        log.exception("longpoll crashed: %s", e)
        _wx["running"] = False


async def _outbox_watcher(account: dict):
    """Watch every session's outbox; forward fresh replies back to the WeChat
    peer that most recently sent INTO that session. _wx['alias_peer'] tracks
    that mapping (set by _inbound_longpoll when a WeChat message arrives).
    """
    token = account["bot_token"]
    base_url = account["baseurl"]
    log.info("weixin outbox_watcher starting (multi-session)")
    try:
        async with wx._build_session() as session:
            while _wx.get("running"):
                await asyncio.sleep(0.5)
                # Walk all session outboxes; SESSIONS_ROOT may grow at runtime
                # if user runs /new in another channel.
                for sess in sx.list_sessions():
                    alias = sess["alias"]
                    p = outbox_for(alias)
                    if not p.exists():
                        continue
                    try:
                        content = p.read_text(encoding="utf-8").strip()
                    except Exception:
                        continue
                    if not content:
                        continue
                    lines = content.split("\n", 1)
                    stamp = lines[0] if content.startswith("[") else ""
                    reply = lines[1] if len(lines) > 1 else content
                    fp = stamp + "|" + reply[:120]
                    if _outbox_seen.get(alias) == fp:
                        continue

                    peer = (_wx.get("alias_peer") or {}).get(alias)
                    if not peer:
                        # No WeChat user has spoken into this session yet —
                        # browser still gets it via /poll. Mark seen so we
                        # don't re-check forever.
                        _outbox_seen[alias] = fp
                        continue
                    ctx_tok = wxs.get_context_token(peer)
                    try:
                        resp = await wx.send_text(
                            session, base_url=base_url, token=token,
                            to_user_id=peer, text=reply, context_token=ctx_tok,
                        )
                        ret = resp.get("ret")
                        if ret not in (0, None):
                            log.warning("send_text ret=%s errmsg=%s",
                                        ret, resp.get("errmsg"))
                        else:
                            log.info("weixin out[%s] to=%s chars=%d",
                                     alias, peer[:8], len(reply))
                    except Exception as e:
                        log.warning("send_text failed: %s", e)
                    _outbox_seen[alias] = fp
    except asyncio.CancelledError:
        log.info("outbox_watcher cancelled")
        raise
    except Exception as e:
        log.exception("outbox_watcher crashed: %s", e)


async def _bootstrap_weixin():
    """On server startup, resume long-poll if we have a stored account."""
    acct = wxs.load_account()
    if acct and acct.get("bot_token"):
        log.info("weixin: resuming saved account %s", acct.get("ilink_bot_id"))
        _start_runtime_tasks(acct)


# ── Autospawn daemon worker ─────────────────────────────────────────────
# sessions._cmd_pick_proj writes to chat_sessions/_autospawn_queue.jsonl
# when the user picks an offline / new project. We drain that file here
# and spawn `python claude_daemon.py <alias> <cwd>` for each entry.

import subprocess  # noqa: E402

_AUTOSPAWN_QUEUE = sx.SESSIONS_ROOT / "_autospawn_queue.jsonl"
_autospawn_running: set[str] = set()  # aliases already spawned in this process lifetime


def _spawn_daemon_detached(alias: str, cwd: str) -> int | None:
    """Spawn `python claude_daemon.py <alias> <cwd>` detached from web_server.
    Returns the PID of the spawned process, or None on failure.

    Windows: use CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW so the daemon
    survives web_server restart and doesn't pop a console window.
    Unix: use start_new_session to detach from web_server's process group.
    """
    daemon_script = str(sx.ROOT / "claude_daemon.py")
    log_path = sx.session_dir(alias) / "daemon_stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(log_path, "a", encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("autospawn[%s]: could not open log: %s", alias, e)
        return None
    kwargs: dict = {
        "stdout": f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(sx.ROOT),  # daemon's CWD = project root; it interprets argv[2] as the spawn cwd
        "close_fds": True,
    }
    if os.name == "nt":
        DETACHED = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = DETACHED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            ["python", daemon_script, alias, cwd],
            **kwargs,
        )
        log.info("autospawn[%s]: spawned pid=%s cwd=%s", alias, proc.pid, cwd)
        return proc.pid
    except Exception as e:
        log.warning("autospawn[%s]: spawn failed: %s", alias, e)
        return None


async def _autospawn_worker():
    """Drain _autospawn_queue.jsonl periodically. For each entry, spawn a
    daemon if (a) we haven't already spawned this alias in this lifetime,
    and (b) no live daemon exists for it.
    """
    log.info("autospawn worker starting")
    try:
        while True:
            await asyncio.sleep(0.5)
            if not _AUTOSPAWN_QUEUE.exists():
                continue
            try:
                lines = _AUTOSPAWN_QUEUE.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            if not lines:
                continue
            # Read-then-truncate (last writer wins for any race; queue is
            # idempotent because we dedupe via _autospawn_running below)
            try:
                _AUTOSPAWN_QUEUE.write_text("", encoding="utf-8")
            except Exception:
                pass
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                alias = rec.get("alias")
                cwd = rec.get("cwd")
                if not alias or not cwd:
                    continue
                if alias in _autospawn_running:
                    log.info("autospawn[%s]: already spawned in this lifetime, skip", alias)
                    continue
                # Check if a live daemon already exists for this alias
                m = sx._load_meta_for(alias) or {}
                daemon_pid = m.get("daemon_pid")
                if daemon_pid and sx._pid_alive(daemon_pid):
                    log.info("autospawn[%s]: daemon pid=%s already alive, skip", alias, daemon_pid)
                    _autospawn_running.add(alias)
                    continue
                pid = _spawn_daemon_detached(alias, cwd)
                if pid:
                    _autospawn_running.add(alias)
    except asyncio.CancelledError:
        log.info("autospawn worker cancelled")
        raise
    except Exception as e:
        log.exception("autospawn worker crashed: %s", e)


# /projects: list workspace projects for the browser UI
async def list_projects_route(request):
    return JSONResponse({
        "workspace_roots": [str(r) for r in sx.get_workspace_roots()],
        "projects": sx.list_projects(),
    })


async def get_config_route(request):
    cfg = sx.load_config()
    # Return the raw configured list (not just existing dirs) so the editor
    # can show entries that are misconfigured / missing too.
    return JSONResponse({
        "workspace_roots": cfg.get("workspace_roots") or [],
        "existing": [str(r) for r in sx.get_workspace_roots()],
    })


async def update_workspace_route(request):
    """POST {"workspace_roots": [...]}  → validate, save, return updated config.

    Each path is normalized but NOT required to exist on disk — user may be
    pre-configuring a path that'll be mounted later. The frontend should
    visually flag missing ones based on /config.existing.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "reason": "invalid JSON"}, status_code=400)
    raw = body.get("workspace_roots")
    if not isinstance(raw, list):
        return JSONResponse({"ok": False, "reason": "workspace_roots must be a list"}, status_code=400)
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        # Normalize to forward slashes for cross-platform consistency in the
        # config file; Path() accepts both.
        s = s.replace("\\", "/").rstrip("/")
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(s)
    cfg = sx.load_config()
    cfg["workspace_roots"] = cleaned
    try:
        sx.save_config(cfg)
    except Exception as e:
        log.warning("save_config failed: %s", e)
        return JSONResponse({"ok": False, "reason": str(e)}, status_code=500)
    log.info("config: workspace_roots updated → %d entries", len(cleaned))
    return JSONResponse({
        "ok": True,
        "workspace_roots": cleaned,
        "existing": [str(r) for r in sx.get_workspace_roots()],
    })


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app):
    # Startup: resume saved WeChat session if any
    await _bootstrap_weixin()
    # Startup: autospawn worker (drains _autospawn_queue.jsonl)
    _wx["tasks"].append(asyncio.create_task(_autospawn_worker(), name="autospawn"))
    yield
    # Shutdown: cancel background tasks
    _cancel_tasks_named("longpoll", "outbox_watch", "qr_login", "autospawn")


app = Starlette(
    routes=[
        Route("/", index),
        Route("/history", get_history),
        Route("/send", send_message, methods=["POST"]),
        Route("/poll", poll),
        Route("/relay-push", relay_push, methods=["POST"]),
        Route("/sessions", list_sessions_route),
        Route("/projects", list_projects_route),
        Route("/config", get_config_route),
        Route("/config/workspace", update_workspace_route, methods=["POST"]),
        Route("/weixin", weixin_page),
        Route("/weixin/status", weixin_status),
        Route("/weixin/qr/start", weixin_qr_start, methods=["POST"]),
        Route("/weixin/disconnect", weixin_disconnect, methods=["POST"]),
    ],
    lifespan=_lifespan,
)


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("web server starting on 127.0.0.1:8765")
    log.info("sessions_root=%s current=%s", sx.SESSIONS_ROOT, sx.get_current())
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
