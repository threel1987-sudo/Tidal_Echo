#!/usr/bin/env python3
r"""
confirm_dev_channel_win.py — Windows 上自动确认 Claude Code 的 DevChannelsDialog。

只有走「Claude Code + channel/ 插件」这条路才需要。CC 用
    claude --dangerously-load-development-channels server:companion
启动时每次都会弹:

    WARNING: Loading development channels
      1. I am using this for local development   ← 默认高亮,按 Enter 即过
      2. Exit

自建本地 server: 频道进不了 channel allowlist(需 Team/Enterprise),这个框
不吃 --dangerously-skip-permissions,也没有 env/settings 能静默它。无人值守
(开机自启 / 自动重启)时会一直卡在这。

办法:启动 CC 后,往它的子控制台注入一个回车替它确认。原理是
AttachConsole(pid) + CreateFileW("CONIN$") + WriteConsoleInputW。

> Linux / macOS 别用这个 —— 用 tmux 更干净:
>     tmux new-session -d -s cc 'claude --dangerously-load-development-channels server:companion'
>     sleep 3 && tmux send-keys -t cc Enter

用法(Windows):
    # A. 当 launcher:开一个独立控制台跑 CC,并自动确认(适合无人值守)
    python confirm_dev_channel_win.py -- claude --dangerously-load-development-channels server:companion

    # B. 对一个已经在跑的 CC 进程确认(适合从无控制台的服务/调度里调)
    python confirm_dev_channel_win.py --pid 12345
"""

import sys
import time
import ctypes
from ctypes import wintypes

if sys.platform != "win32":
    sys.exit("这个脚本只用于 Windows;Linux/macOS 请用 tmux send-keys(见文件头注释)。")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x00000003
OPEN_EXISTING = 0x00000003
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
KEY_EVENT = 0x0001
VK_RETURN = 0x0D

kernel32.AttachConsole.argtypes = [wintypes.DWORD]
kernel32.AttachConsole.restype = wintypes.BOOL
kernel32.FreeConsole.restype = wintypes.BOOL
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", wintypes.WCHAR),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class INPUT_RECORD(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]
    _anonymous_ = ("u",)
    _fields_ = [("EventType", wintypes.WORD), ("u", _U)]


def send_enter_to_console(pid: int) -> bool:
    """往 pid 的控制台输入缓冲送一次回车(down+up)。成功返回 True。"""
    kernel32.FreeConsole()                       # 先脱离自己的 console
    if not kernel32.AttachConsole(pid):          # 接上目标进程的 console
        return False
    handle = INVALID_HANDLE_VALUE
    try:
        handle = kernel32.CreateFileW("CONIN$", GENERIC_WRITE, FILE_SHARE_RW,
                                      None, OPEN_EXISTING, 0, None)
        if handle == INVALID_HANDLE_VALUE:
            return False
        recs = (INPUT_RECORD * 2)()
        for i, down in enumerate((1, 0)):        # 一次完整按键 = 按下 + 抬起
            recs[i].EventType = KEY_EVENT
            ke = recs[i].KeyEvent
            ke.bKeyDown = down
            ke.wRepeatCount = 1
            ke.wVirtualKeyCode = VK_RETURN
            ke.wVirtualScanCode = 0x1C
            ke.uChar = "\r"
            ke.dwControlKeyState = 0
        written = wintypes.DWORD(0)
        ok = kernel32.WriteConsoleInputW(handle, recs, 2, ctypes.byref(written))
        return bool(ok)
    finally:
        if handle and handle != INVALID_HANDLE_VALUE:
            kernel32.CloseHandle(handle)
        kernel32.FreeConsole()                   # 还回自己的 console


def auto_confirm(pid: int, window: int = 20, interval: int = 2) -> None:
    """在 window 秒内每 interval 秒送一次回车;框渲染出来那一下即被读掉确认。
    多送的空回车落在输入框无害。全程不抛错(送键失败最坏退回人工按一次)。"""
    deadline = time.time() + window
    sent = 0
    while time.time() < deadline:
        try:
            if send_enter_to_console(pid):
                sent += 1
        except Exception:
            pass
        time.sleep(interval)
    print(f"[devchan] auto-Enter done (sent={sent})", file=sys.stderr, flush=True)


def main(argv: list) -> int:
    if "--pid" in argv:
        pid = int(argv[argv.index("--pid") + 1])
        auto_confirm(pid)
        return 0

    # launcher 模式:-- 之后是要启动的命令
    if "--" in argv:
        cmd = argv[argv.index("--") + 1:]
    else:
        cmd = argv[1:]
    if not cmd:
        print(__doc__)
        return 2

    import subprocess
    import threading
    CREATE_NEW_CONSOLE = 0x00000010              # 给 CC 独立 console,便于干净地 Attach
    proc = subprocess.Popen(cmd, creationflags=CREATE_NEW_CONSOLE)
    print(f"[devchan] launched pid={proc.pid}: {' '.join(cmd)}", file=sys.stderr, flush=True)
    threading.Thread(target=auto_confirm, args=(proc.pid,), daemon=True).start()
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
