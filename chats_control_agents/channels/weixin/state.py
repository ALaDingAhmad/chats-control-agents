"""
Persistent state for WeChat (iLink) integration.

Stores:
  - The bot account credentials so we can resume long-poll after restart.
  - Per-peer context_token (iLink requires echoing the most recent token
    for each conversation; without it replies vanish).

State directory: <project_root>/weixin_state/
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, Optional

from ...core.paths import ROOT as PROJECT_ROOT

STATE_DIR = PROJECT_ROOT / "weixin_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

ACCOUNT_FILE = STATE_DIR / "account.json"
CONTEXT_TOKEN_FILE = STATE_DIR / "context_tokens.json"

_lock = threading.Lock()


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── Account credentials ──────────────────────────────────────────────────
def save_account(account: dict) -> None:
    """Persist {ilink_bot_id, bot_token, baseurl, ilink_user_id, ...}."""
    with _lock:
        _atomic_write(ACCOUNT_FILE, account)


def load_account() -> Optional[dict]:
    if not ACCOUNT_FILE.exists():
        return None
    try:
        return json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_account() -> None:
    with _lock:
        if ACCOUNT_FILE.exists():
            ACCOUNT_FILE.unlink()


# ── Context tokens (per peer) ────────────────────────────────────────────
def get_context_token(peer_id: str) -> Optional[str]:
    if not CONTEXT_TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(CONTEXT_TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data.get(peer_id)


def set_context_token(peer_id: str, token: str) -> None:
    if not token:
        return
    with _lock:
        data: Dict[str, str] = {}
        if CONTEXT_TOKEN_FILE.exists():
            try:
                data = json.loads(CONTEXT_TOKEN_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[peer_id] = token
        _atomic_write(CONTEXT_TOKEN_FILE, data)
