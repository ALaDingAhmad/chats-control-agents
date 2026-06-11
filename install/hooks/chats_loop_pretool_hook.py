"""
PreToolUse hook for chats-loop mode.

Fires immediately BEFORE Claude executes mcp__cca-msg__send_chat_response.
At this moment, Claude's full assistant content for this turn — including
any narration like "let me check X first" — has already been written to
the transcript jsonl. We harvest that narration and push it to the web
server so the browser sees the full turn, not just the final reply.

Configured in ~/.claude/settings.json hooks.PreToolUse with matcher
`mcp__cca-msg__send_chat_response` — that means this hook ONLY fires for
that one tool, never on Read/Bash/wait_for_message etc. No marker file
needed.

Stdin (Claude Code → hook) JSON:
  {
    "session_id": "...",
    "transcript_path": ".../<session>.jsonl",
    "hook_event_name": "PreToolUse",
    "tool_name": "mcp__cca-msg__send_chat_response",
    "tool_input": {"reply": "<the reply Claude is about to send>"},
    "tool_use_id": "toolu_..."
  }

Output: nothing. The tool then runs normally — send_chat_response writes
the reply to chat_outbox.txt and the browser /poll picks it up as usual.

Log: ~/.claude/hooks/chats_loop_pretool_hook.log
"""
import json
import logging
import sys
import urllib.request
from pathlib import Path

LOG_PATH = Path.home() / ".claude" / "hooks" / "chats_loop_pretool_hook.log"
# 端口在 install/install.py 装的时候按 config.json:web_port 渲染。
# 源文件里写默认 8765；改了 config.json 必须重跑 `install/install.py --hook`，
# 否则这里仍是老端口、hook 就会 push 到错的 server。
WEB_PUSH_URL = "http://127.0.0.1:8765/relay-push"  # CHATS_BRIDGE_WEB_PORT_LINE

logging.basicConfig(
    filename=str(LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("pretool")


def main():
    try:
        raw = sys.stdin.read()
    except Exception as e:
        log.exception("stdin read failed: %s", e)
        return
    if not raw.strip():
        log.warning("empty stdin")
        return

    try:
        payload = json.loads(raw)
    except Exception as e:
        log.exception("parse stdin failed: %s", e)
        return

    transcript_path = payload.get("transcript_path") or ""
    tool_use_id = payload.get("tool_use_id") or ""
    tool_input = payload.get("tool_input") or {}
    reply_about_to_send = (tool_input.get("reply") or "").strip()
    session_id = payload.get("session_id", "?")

    if not transcript_path or not Path(transcript_path).exists():
        log.warning("session=%s transcript not found at %s", session_id, transcript_path)
        return

    narration = _harvest_turn_narration(
        Path(transcript_path), tool_use_id, reply_about_to_send
    )
    if not narration:
        log.info("session=%s no narration to push (clean turn)", session_id)
        return

    log.info("session=%s pushing narration %d chars", session_id, len(narration))
    try:
        body = json.dumps({"text": narration, "source": "pre_send_hook"}).encode("utf-8")
        req = urllib.request.Request(
            WEB_PUSH_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            log.info("  pushed, HTTP %d", resp.status)
    except Exception as e:
        log.warning("push failed: %s", e)


def _harvest_turn_narration(
    transcript_path: Path,
    target_tool_use_id: str,
    reply_about_to_send: str,
) -> str:
    """Walk transcript backwards from the assistant message that contains
    target_tool_use_id. Collect assistant `text` blocks emitted in this turn
    (since the most recent user message OR previous send_chat_response).
    Returns the concatenated narration text minus the reply itself.
    """
    try:
        lines = transcript_path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        log.exception("read transcript failed: %s", e)
        return ""

    # Locate the assistant entry whose content includes our tool_use_id.
    target_idx = None
    if target_tool_use_id:
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i].strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("type") != "assistant":
                continue
            content = (msg.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("id") == target_tool_use_id:
                    target_idx = i
                    break
            if target_idx is not None:
                break

    if target_idx is None:
        # Fallback: use the last assistant entry
        for i in range(len(lines) - 1, -1, -1):
            try:
                msg = json.loads(lines[i])
                if msg.get("type") == "assistant":
                    target_idx = i
                    break
            except Exception:
                continue
    if target_idx is None:
        return ""

    # Walk backwards from target_idx, collecting assistant text blocks.
    # Boundary is "the user message that opened THIS turn". For a relay loop
    # that user message is the `tool_result` from the most recent
    # mcp__cca-msg__wait_for_message — that result carries the web user's
    # incoming text. Until we cross such a user message, every assistant
    # `text` belongs to this turn and should be collected.
    # We need a way to know which wait_for_message tool_use opened THIS turn.
    # First scan back from target to find that wait_for_message tool_use_id.
    wait_id = None
    for i in range(target_idx, -1, -1):
        try:
            m = json.loads(lines[i])
        except Exception:
            continue
        if m.get("type") != "assistant":
            continue
        content = (m.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        for c in content:
            if (
                isinstance(c, dict)
                and c.get("type") == "tool_use"
                and c.get("name") == "mcp__cca-msg__wait_for_message"
            ):
                wait_id = c.get("id")
                break
        if wait_id is not None:
            break

    collected: list[str] = []
    for i in range(target_idx, -1, -1):
        line = lines[i].strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mtype = msg.get("type")

        if mtype in ("user", "human"):
            # Only treat this as a turn boundary if it carries the tool_result
            # for the wait_for_message that opened this turn. Other user
            # messages (Bash tool_results, Read tool_results, etc.) are
            # internal steps within the turn — keep walking past them.
            content = (msg.get("message") or {}).get("content") or []
            is_boundary = False
            if wait_id and isinstance(content, list):
                for c in content:
                    if (
                        isinstance(c, dict)
                        and c.get("type") == "tool_result"
                        and c.get("tool_use_id") == wait_id
                    ):
                        is_boundary = True
                        break
            elif isinstance(content, str):
                # Plain text user message — definitely a boundary
                is_boundary = True
            if is_boundary:
                break
            continue

        if mtype != "assistant":
            continue
        content = (msg.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        # Collect text blocks from this entry (in their natural order).
        entry_texts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = (c.get("text") or "").strip()
                if t:
                    entry_texts.append(t)
        if entry_texts:
            # Prepend (we're walking backwards)
            collected.insert(0, "\n".join(entry_texts))

    if not collected:
        return ""

    narration = "\n\n".join(collected).strip()

    # If the narration accidentally matches the reply itself, suppress —
    # Claude sometimes also writes the answer as terminal text.
    if reply_about_to_send and narration == reply_about_to_send:
        return ""
    # If narration ENDS with the reply, strip it (Claude wrote the answer
    # in terminal then also passed it to send_chat_response).
    if reply_about_to_send and narration.endswith(reply_about_to_send):
        narration = narration[: -len(reply_about_to_send)].strip()
    return narration


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("hook crashed: %s", e)
