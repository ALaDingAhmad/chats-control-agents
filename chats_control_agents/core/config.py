"""Bridge config: workspace_roots and any future global settings.

Stored as `config.json` at project root. Schema is intentionally permissive —
unknown keys are kept on round-trip so manual edits aren't lost.
"""
from __future__ import annotations

import json
from pathlib import Path

from .paths import CONFIG_FILE


DEFAULT_CONFIG: dict = {
    "workspace_roots": [],
    "web_port": 8765,
}


def load_config() -> dict:
    """Read config.json. Returns defaults if file is missing or malformed.
    Never raises — caller can rely on a usable dict back.
    """
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return dict(DEFAULT_CONFIG)
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def get_web_port() -> int:
    """Read web_server 监听端口。缺省 8765。

    改了之后必须重启 web_server。如果改的同时还跑着 hook（child claude
    PreToolUse 回 `/relay-push`），必须重跑 `install/install.py --hook`
    让 hook 副本里的端口也跟着变——hook 装到 ~/.claude/hooks/ 是冻结
    副本，不动态读 config.json。
    """
    cfg = load_config()
    raw = cfg.get("web_port", 8765)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return 8765
    if not (1 <= port <= 65535):
        return 8765
    return port


def get_workspace_roots() -> list[Path]:
    """Return validated, deduplicated, existing workspace root paths.
    Strings in config that don't resolve to real directories are dropped.
    """
    cfg = load_config()
    out: list[Path] = []
    seen: set[str] = set()
    for raw in cfg.get("workspace_roots") or []:
        try:
            p = Path(raw).resolve()
        except Exception:
            continue
        key = str(p).lower()
        if key in seen:
            continue
        if p.exists() and p.is_dir():
            out.append(p)
            seen.add(key)
    return out
