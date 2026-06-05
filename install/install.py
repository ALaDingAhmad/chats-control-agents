"""
Installer for agent-bridge artefacts that live OUTSIDE the repo
(~/.claude.json, ~/.claude/skills, ~/.claude/hooks, ~/.claude/settings.json).

Usage:
    python install/install.py            # install all components
    python install/install.py --mcp      # register web-chat MCP server
    python install/install.py --skill    # install web-relay skill
    python install/install.py --hook     # install + register PreToolUse hook
    python install/install.py --dry-run  # show planned changes, write nothing
    python install/install.py --uninstall

Safe by design:
  - Every JSON edit takes a timestamped backup first (.bak-YYYYMMDD-HHMMSS).
  - JSON edits are MERGES (preserve unrelated fields).
  - File copies overwrite, but the destination is backed up if it differs
    from what we'd write.
  - Path to mcp_bridge.py is computed from THIS file's location, so the
    installer works from any clone path.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


# Source artefacts (this file lives at <repo>/install/install.py)
INSTALL_DIR = Path(__file__).resolve().parent
REPO_ROOT = INSTALL_DIR.parent
MCP_BRIDGE_ABS_PATH = REPO_ROOT / "agent_bridge" / "backends" / "claude_code" / "mcp_bridge.py"

# Targets in user's home
HOME = Path.home()
CLAUDE_JSON = HOME / ".claude.json"                      # MCP servers + general
SETTINGS_JSON = HOME / ".claude" / "settings.json"       # hooks + permissions
SKILLS_DIR = HOME / ".claude" / "skills"
HOOKS_DIR = HOME / ".claude" / "hooks"

# Identifiers used for idempotency
MCP_SERVER_NAME = "web-chat"
SKILL_NAME = "web-relay"
HOOK_FILENAME = "web_relay_pretool_hook.py"
HOOK_MATCHER = "mcp__web-chat__send_chat_response"

# ── Logging-ish helpers ──────────────────────────────────────────────────
def info(msg: str) -> None:
    print(f"  {msg}")

def section(name: str) -> None:
    print(f"\n[{name}]")

def warn(msg: str) -> None:
    print(f"  ⚠️  {msg}")

def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


# ── Helpers ──────────────────────────────────────────────────────────────
def _now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup_file(path: Path, dry: bool = False) -> Path | None:
    if not path.exists():
        return None
    bak = path.with_name(path.name + f".bak-{_now_stamp()}")
    if dry:
        return bak  # caller logs the planned name; no file written
    shutil.copy2(path, bak)
    return bak


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"ERROR: failed to parse {path}: {e}")


def _write_json(path: Path, data: dict, dry: bool) -> None:
    blob = json.dumps(data, ensure_ascii=False, indent=2)
    if dry:
        info(f"[dry-run] would write {path} ({len(blob)} bytes)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(blob + "\n", encoding="utf-8")


# ── Component: MCP registration ──────────────────────────────────────────
def install_mcp(dry: bool) -> None:
    section("MCP (web-chat in ~/.claude.json)")
    if not MCP_BRIDGE_ABS_PATH.exists():
        raise SystemExit(f"ERROR: mcp_bridge.py not found at {MCP_BRIDGE_ABS_PATH}")
    desired = {
        "type": "stdio",
        "command": "python",
        "args": [str(MCP_BRIDGE_ABS_PATH).replace("\\", "/")],
        "env": {},
    }
    data = _load_json(CLAUDE_JSON)
    servers = data.setdefault("mcpServers", {})
    existing = servers.get(MCP_SERVER_NAME)
    if existing == desired:
        ok(f"{MCP_SERVER_NAME} already registered with the right path, no change")
        return
    if existing:
        info(f"existing {MCP_SERVER_NAME}: {existing.get('args')}")
        info(f"new      {MCP_SERVER_NAME}: {desired['args']}")
    bak = _backup_file(CLAUDE_JSON, dry)
    if bak and not dry:
        ok(f"backup → {bak.name}")
    servers[MCP_SERVER_NAME] = desired
    _write_json(CLAUDE_JSON, data, dry)
    if not dry:
        ok(f"{MCP_SERVER_NAME} → args[0]={desired['args'][0]}")


def uninstall_mcp(dry: bool) -> None:
    section("MCP (uninstall)")
    data = _load_json(CLAUDE_JSON)
    servers = data.get("mcpServers", {})
    if MCP_SERVER_NAME not in servers:
        ok(f"{MCP_SERVER_NAME} not present, nothing to remove")
        return
    bak = _backup_file(CLAUDE_JSON, dry)
    if bak and not dry:
        ok(f"backup → {bak.name}")
    del servers[MCP_SERVER_NAME]
    _write_json(CLAUDE_JSON, data, dry)
    if not dry:
        ok(f"removed {MCP_SERVER_NAME}")


# ── Component: skill ─────────────────────────────────────────────────────
def install_skill(dry: bool) -> None:
    section(f"Skill ({SKILL_NAME} in ~/.claude/skills/)")
    src = INSTALL_DIR / "skills" / SKILL_NAME
    if not src.is_dir():
        raise SystemExit(f"ERROR: source skill dir missing: {src}")
    dst = SKILLS_DIR / SKILL_NAME
    if dst.exists():
        # Compare each file; back up if anything differs.
        any_diff = False
        for fsrc in src.rglob("*"):
            if not fsrc.is_file():
                continue
            rel = fsrc.relative_to(src)
            fdst = dst / rel
            if not fdst.exists() or fdst.read_bytes() != fsrc.read_bytes():
                any_diff = True
                break
        if not any_diff:
            ok("skill already installed and up-to-date")
            return
        bak = dst.with_name(dst.name + f".bak-{_now_stamp()}")
        if dry:
            info(f"[dry-run] would back up {dst} → {bak.name}")
        else:
            shutil.copytree(dst, bak)
            ok(f"backup → {bak.name}")
            shutil.rmtree(dst)
    if dry:
        info(f"[dry-run] would copy {src} → {dst}")
        return
    shutil.copytree(src, dst)
    ok(f"{src.name} → {dst}")


def uninstall_skill(dry: bool) -> None:
    section(f"Skill (uninstall)")
    dst = SKILLS_DIR / SKILL_NAME
    if not dst.exists():
        ok("skill not installed")
        return
    bak = dst.with_name(dst.name + f".bak-{_now_stamp()}")
    if dry:
        info(f"[dry-run] would move {dst} → {bak.name}")
        return
    shutil.move(str(dst), str(bak))
    ok(f"moved to backup: {bak.name}")


# ── Component: hook script + settings.json registration ──────────────────
def install_hook(dry: bool) -> None:
    section("Hook (web_relay_pretool_hook.py + ~/.claude/settings.json)")
    src = INSTALL_DIR / "hooks" / HOOK_FILENAME
    if not src.is_file():
        raise SystemExit(f"ERROR: source hook missing: {src}")
    dst_script = HOOKS_DIR / HOOK_FILENAME
    # 1. copy script
    if dst_script.exists() and dst_script.read_bytes() == src.read_bytes():
        ok("hook script already up-to-date")
    else:
        if dst_script.exists():
            bak = _backup_file(dst_script, dry)
            if bak and not dry:
                ok(f"backup → {bak.name}")
        if dry:
            info(f"[dry-run] would copy {src.name} → {dst_script}")
        else:
            dst_script.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_script)
            ok(f"copied → {dst_script}")
    # 2. settings.json registration
    data = _load_json(SETTINGS_JSON)
    hooks_root = data.setdefault("hooks", {})
    pretool = hooks_root.setdefault("PreToolUse", [])
    desired_command = f"python {dst_script.as_posix()}"
    existing_entry = None
    for entry in pretool:
        if entry.get("matcher") == HOOK_MATCHER:
            existing_entry = entry
            break
    if existing_entry:
        existing_cmds = [h.get("command") for h in existing_entry.get("hooks", [])]
        if desired_command in existing_cmds:
            ok("settings.json already registers this hook")
            return
        bak = _backup_file(SETTINGS_JSON, dry)
        if bak and not dry:
            ok(f"backup → {bak.name}")
        existing_entry.setdefault("hooks", []).append({
            "type": "command", "command": desired_command, "timeout": 10,
        })
    else:
        bak = _backup_file(SETTINGS_JSON, dry)
        if bak and not dry:
            ok(f"backup → {bak.name}")
        pretool.append({
            "matcher": HOOK_MATCHER,
            "hooks": [
                {"type": "command", "command": desired_command, "timeout": 10}
            ],
        })
    _write_json(SETTINGS_JSON, data, dry)
    if not dry:
        ok(f"PreToolUse[matcher={HOOK_MATCHER}] → {desired_command}")


def uninstall_hook(dry: bool) -> None:
    section("Hook (uninstall)")
    # 1. settings.json: drop the registration
    data = _load_json(SETTINGS_JSON)
    pretool = data.get("hooks", {}).get("PreToolUse", [])
    survivors = [e for e in pretool if e.get("matcher") != HOOK_MATCHER]
    if len(survivors) != len(pretool):
        bak = _backup_file(SETTINGS_JSON, dry)
        if bak and not dry:
            ok(f"backup → {bak.name}")
        data["hooks"]["PreToolUse"] = survivors
        _write_json(SETTINGS_JSON, data, dry)
        if not dry:
            ok("removed registration from settings.json")
    else:
        ok("no registration to remove in settings.json")
    # 2. remove the script
    dst_script = HOOKS_DIR / HOOK_FILENAME
    if dst_script.exists():
        bak = _backup_file(dst_script, dry)
        if bak and not dry:
            ok(f"backup → {bak.name}")
        if dry:
            info(f"[dry-run] would remove {dst_script}")
        else:
            dst_script.unlink()
            ok(f"removed {dst_script}")
    else:
        ok("hook script not present")


# ── CLI ──────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mcp",   action="store_true", help="register web-chat MCP server")
    parser.add_argument("--skill", action="store_true", help="install web-relay skill")
    parser.add_argument("--hook",  action="store_true", help="install + register PreToolUse hook")
    parser.add_argument("--dry-run", action="store_true", help="show planned changes, write nothing")
    parser.add_argument("--uninstall", action="store_true", help="reverse the install")
    args = parser.parse_args()

    # No specific component → all three
    selected_any = args.mcp or args.skill or args.hook
    do_mcp = args.mcp or not selected_any
    do_skill = args.skill or not selected_any
    do_hook = args.hook or not selected_any

    mode = "UNINSTALL" if args.uninstall else "INSTALL"
    dry = " (dry-run)" if args.dry_run else ""
    print(f"=== agent-bridge {mode}{dry} ===")
    print(f"repo:  {REPO_ROOT}")
    print(f"home:  {HOME}")

    actions = []
    if args.uninstall:
        if do_hook:  actions.append(uninstall_hook)
        if do_skill: actions.append(uninstall_skill)
        if do_mcp:   actions.append(uninstall_mcp)
    else:
        if do_mcp:   actions.append(install_mcp)
        if do_skill: actions.append(install_skill)
        if do_hook:  actions.append(install_hook)

    try:
        for fn in actions:
            fn(args.dry_run)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        return 2

    print(f"\n=== done ===")
    print("Next: restart any Claude windows you want the new MCP/hook to take effect for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
