"""Safely kill daemon-spawned claude.exe + descendant processes.

Strategy:
  1. Read chat_sessions/<alias>/spawned_pids.jsonl for every alias.
  2. For each logged (pid, create_time), keep it only if a live process
     matches BOTH (psutil rejects PID-recycled strangers via create_time).
  3. Recursively collect all descendants (mcp_bridge, cmd.exe shims, ...).
  4. Kill the tree.

What this does NOT touch:
  - User-launched claude.exe (never logged in spawned_pids.jsonl)
  - The web_server.py process (separate concern; use `python -m scripts.stop_web`)
  - This script's own python process (unless someone logged it, which would
    be a bug)

Usage:
  python _kill_daemon_children.py            # dry-run: print what would die
  python _kill_daemon_children.py --doit     # actually kill
  python _kill_daemon_children.py --alias X  # only kill children of alias X
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import sessions as sx  # noqa: E402

try:
    import psutil
except ImportError:
    print("ERROR: psutil not installed. Run: pip install -r requirements.txt",
          file=sys.stderr)
    sys.exit(2)


def _describe(p: psutil.Process) -> str:
    try:
        name = p.name()
    except Exception:
        name = "?"
    try:
        cmd = " ".join(p.cmdline())[:120]
    except Exception:
        cmd = ""
    try:
        ct = p.create_time()
    except Exception:
        ct = 0
    return f"PID={p.pid:<6} name={name:<20} ct={ct:.0f} cmd={cmd}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doit", action="store_true",
                        help="actually kill (default: dry-run)")
    parser.add_argument("--alias", default=None,
                        help="restrict to one alias (default: all)")
    args = parser.parse_args()

    if args.alias:
        # Filter logged records to just this alias
        all_recs = [r for r in sx.list_logged_child_records()
                    if r.get("_alias") == args.alias]
        if not all_recs:
            print(f"No spawned_pids.jsonl entries for alias '{args.alias}'.")
            return 0
        # Replay list_daemon_child_pids() filter logic on this subset
        roots: set[int] = set()
        for rec in all_recs:
            pid, logged_ct = rec.get("pid"), rec.get("create_time")
            if not pid:
                continue
            try:
                proc = psutil.Process(pid)
                if logged_ct is None or abs(proc.create_time() - logged_ct) < 1.0:
                    roots.add(pid)
            except psutil.NoSuchProcess:
                continue
        # Expand to descendants
        tree: set[int] = set(roots)
        for pid in roots:
            try:
                for c in psutil.Process(pid).children(recursive=True):
                    tree.add(c.pid)
            except Exception:
                pass
    else:
        tree = sx.list_daemon_descendants()

    if not tree:
        print("No live daemon-spawned processes found. Nothing to kill.")
        return 0

    # Sanity guard: never kill our own pid
    my_pid = os.getpid()
    if my_pid in tree:
        print(f"WARNING: own pid {my_pid} appears in target set, excluding.")
        tree.discard(my_pid)

    print(f"Target processes ({len(tree)} total):")
    procs: list[psutil.Process] = []
    for pid in sorted(tree):
        try:
            p = psutil.Process(pid)
            print(f"  {_describe(p)}")
            procs.append(p)
        except psutil.NoSuchProcess:
            print(f"  PID={pid:<6} (gone)")

    if not args.doit:
        print("\n(dry-run; pass --doit to actually kill)")
        return 0

    print("\nKilling...")
    # Kill leaves first so parents don't auto-respawn / re-claim file handles.
    # psutil.Process.kill() == SIGKILL on Unix, TerminateProcess on Windows.
    procs.sort(key=lambda p: -p.pid)  # rough leaf-first order
    for p in procs:
        try:
            p.kill()
            print(f"  killed PID={p.pid}")
        except psutil.NoSuchProcess:
            print(f"  PID={p.pid} already gone")
        except Exception as e:
            print(f"  PID={p.pid} kill failed: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
