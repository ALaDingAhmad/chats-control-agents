"""停止 start_web_detached 起的后台 web_server。

用法：
    python -m scripts.stop_web

行为：
- 读 web_server.pid → TerminateProcess → 删 pid 文件
- PID 文件不存在或 PID 已死：报告"没在跑"，不报错
"""
from __future__ import annotations

import sys

from chats_control_agents.core.paths import ROOT
from chats_control_agents.core.pid_track import _kill_pid, _pid_alive


PID_FILE = ROOT / "web_server.pid"


def main() -> int:
    if not PID_FILE.exists():
        print("没找到 web_server.pid，web_server 可能没在跑（或者不是 start_web_detached 起的）。")
        return 0

    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception as e:
        print(f"读 pid 文件失败: {e}")
        return 2

    if not _pid_alive(pid):
        print(f"pid={pid} 已经不活了——清理 pid 文件。")
        PID_FILE.unlink()
        return 0

    ok = _kill_pid(pid)
    if not ok:
        print(f"TerminateProcess(pid={pid}) 失败。手动 `taskkill /F /PID {pid}`。")
        return 3

    PID_FILE.unlink()
    print(f"已停 web_server（pid={pid}）。")
    print("注意：daemon 子进程是 detached，不会跟着停——它们仍然托管会话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
