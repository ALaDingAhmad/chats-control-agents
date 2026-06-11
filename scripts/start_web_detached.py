"""后台启动 web_server.py——关掉这个终端它继续活着。

用法：
    python -m scripts.start_web_detached

行为：
- 检查 web_server.pid 是否存在且 PID 还活着 → 拒绝重起
- subprocess.Popen with DETACHED + CREATE_NO_WINDOW + start_new_session
  → 进程脱离当前会话，关 Git Bash / 注销 Windows 都不影响
- stdout/stderr 接到 web_server.log（uvicorn 自己也写这里）
- PID 写到 web_server.pid 给 stop_web.py 用

仅 Windows。Linux/macOS 不在本项目部署目标内。

停止用 `python -m scripts.stop_web`。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from chats_control_agents.core.config import get_web_port
from chats_control_agents.core.paths import ROOT
from chats_control_agents.core.pid_track import _pid_alive


PID_FILE = ROOT / "web_server.pid"
LOG_FILE = ROOT / "web_server.log"


def main() -> int:
    if os.name != "nt":
        print("本脚本只支持 Windows。Linux/macOS 用 nohup / systemd / launchd 自己起。")
        return 2

    # 重起保护
    if PID_FILE.exists():
        try:
            existing = int(PID_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            existing = 0
        if existing and _pid_alive(existing):
            print(f"web_server 已在跑（pid={existing}）。要重起先 `python -m scripts.stop_web`。")
            return 1
        # PID 死了——pid 文件是残留，删掉继续
        try:
            PID_FILE.unlink()
        except Exception:
            pass

    # 起进程：detached + no window + 新会话，关终端不杀
    DETACHED = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000

    try:
        log_f = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"打不开 {LOG_FILE}: {e}")
        return 3

    proc = subprocess.Popen(
        [sys.executable, "-m", "chats_control_agents.web.server"],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(ROOT),
        close_fds=True,
        creationflags=DETACHED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
    )

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    port = get_web_port()
    print(f"web_server 已起在后台。pid={proc.pid}")
    print(f"  log: {LOG_FILE}")
    print(f"  pid: {PID_FILE}")
    print(f"  dashboard: http://127.0.0.1:{port}/")
    print("关掉本终端不会影响这个进程。停止用 `python -m scripts.stop_web`。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
