"""Type into the Claude Code session by writing to its console input buffer.

The old path put text on the clipboard, pulled the session's window to the
front, and sent Ctrl+V and Enter. Under Tabby, Windows Terminal or VS Code the
shell runs through ConPTY, so the console window is a real but HIDDEN
pseudo-console window. SetForegroundWindow on it fails with error 0, so every
message was lost and the caller heard "session not active".

This module goes in the other door. A process can attach to another process's
console with AttachConsole, open that console's input buffer as "CONIN$", and
push key events into it with WriteConsoleInputW. The session reads them as if
they were typed. No window focus, no clipboard, and it works when the console
window is hidden.

AttachConsole swaps the console for the WHOLE calling process, which would take
the bridge's own console and its logging with it. So the attach happens in a
short-lived child process: this file is both an importable module and the child
entry point. The parent spawns it, hands the text over on stdin (never on the
command line, where a long prompt would hit the Windows argument limit and leak
into process lists), and reads back an exit code plus one line on stderr.

Public API:
    find_target()   -> (pid, how) for the session to type into
    inject_text(s)  -> (ok, reason)
    send_escape()   -> (ok, reason)
"""
import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

# claude-voice.ps1 writes the session's own process id here. That is the process
# holding the console we want, so it beats guessing from a window handle.
PID_FILE = os.path.expanduser(r"~\.claude\voice-session.pid")
# Fallback: the window handle the launcher saved. Its owning process id is good
# enough to attach to.
HWND_FILE = os.path.expanduser(r"~\.claude\voice-session.hwnd")

# How long a single injection may take before the child is killed. The bridge is
# on a live phone call, so a hung child must never stall it.
DEFAULT_TIMEOUT = 5.0

CREATE_NO_WINDOW = 0x08000000

_MODE_TEXT = "text"
_MODE_ESCAPE = "escape"


# ---------------------------------------------------------------- target lookup

def _pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _pid_from_file() -> int | None:
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _pid_from_hwnd() -> int | None:
    try:
        with open(HWND_FILE, encoding="utf-8") as f:
            hwnd = int(f.read().strip())
    except (OSError, ValueError):
        return None
    user32 = ctypes.windll.user32
    if not user32.IsWindow(wintypes.HWND(hwnd)):
        return None
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value or None


def find_target() -> tuple[int | None, str]:
    """The process id to attach to, and how it was found.

    Order: the pid file the launcher writes, then the owner of the saved window
    handle. Nothing is guessed: when neither is usable the caller gets None and
    a reason.
    """
    pid = _pid_from_file()
    if pid and _pid_alive(pid):
        return pid, "pid file"
    stale = " (pid file is stale)" if pid else ""

    pid = _pid_from_hwnd()
    if pid and _pid_alive(pid):
        return pid, "window handle owner" + stale
    return None, "no live session process found" + stale


# --------------------------------------------------------------- parent process

def _child_command(pid: int, mode: str) -> list[str]:
    return [sys.executable, os.path.abspath(__file__), "--pid", str(pid), "--mode", mode]


def _run_child(mode: str, payload: str, timeout: float) -> tuple[bool, str]:
    pid, how = find_target()
    if pid is None:
        return False, how

    try:
        proc = subprocess.run(
            _child_command(pid, mode),
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False, f"console injection timed out after {timeout:g}s (pid {pid} via {how})"
    except OSError as exc:
        return False, f"could not start the injector process: {exc}"

    detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    detail = detail[-1] if detail else ""
    if proc.returncode == 0:
        return True, f"pid {pid} via {how}{': ' + detail if detail else ''}"
    return False, f"pid {pid} via {how}: {detail or f'exit code {proc.returncode}'}"


def inject_text(text: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Type text plus Enter into the session's console. Returns (ok, reason)."""
    return _run_child(_MODE_TEXT, text, timeout)


def send_escape(timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Press Escape in the session's console. Returns (ok, reason)."""
    return _run_child(_MODE_ESCAPE, "", timeout)


# ---------------------------------------------------------------- child process
# Everything below runs in the short-lived child, after which the process exits
# and its hijacked console goes with it.

VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
KEY_EVENT = 0x0001

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _CHAR_UNION(ctypes.Union):
    _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", _CHAR_UNION),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _EVENT_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", _KEY_EVENT_RECORD)]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT_UNION)]


def _key_pair(char: str, vk: int = 0) -> list[_INPUT_RECORD]:
    """A key-down and key-up record for one character."""
    out = []
    for down in (True, False):
        rec = _INPUT_RECORD()
        rec.EventType = KEY_EVENT
        rec.Event.KeyEvent.bKeyDown = down
        rec.Event.KeyEvent.wRepeatCount = 1
        rec.Event.KeyEvent.wVirtualKeyCode = vk
        rec.Event.KeyEvent.wVirtualScanCode = 0
        rec.Event.KeyEvent.uChar.UnicodeChar = char
        rec.Event.KeyEvent.dwControlKeyState = 0
        out.append(rec)
    return out


def build_records(mode: str, text: str) -> list[_INPUT_RECORD]:
    """The key events to push, for the text prompt or for Escape."""
    if mode == _MODE_ESCAPE:
        return _key_pair("\x1b", VK_ESCAPE)

    records: list[_INPUT_RECORD] = []
    for char in text:
        # A newline in the middle of a prompt reads as a press of Enter, the
        # same as typing it, so send it as the carriage return the console
        # expects rather than a bare line feed.
        if char in "\r\n":
            records += _key_pair("\r", VK_RETURN)
        else:
            records += _key_pair(char)
    records += _key_pair("\r", VK_RETURN)
    return records


def _child_main(argv: list[str]) -> int:
    args = dict(zip(argv[::2], argv[1::2]))
    try:
        pid = int(args["--pid"])
    except (KeyError, ValueError):
        print("usage: console_inject.py --pid N --mode text|escape (text on stdin)", file=sys.stderr)
        return 2
    mode = args.get("--mode", _MODE_TEXT)
    if mode not in (_MODE_TEXT, _MODE_ESCAPE):
        print(f"unknown mode {mode!r}", file=sys.stderr)
        return 2

    text = "" if mode == _MODE_ESCAPE else sys.stdin.buffer.read().decode("utf-8")
    records = build_records(mode, text)
    if not records:
        print("nothing to send", file=sys.stderr)
        return 1

    kernel32 = ctypes.windll.kernel32
    # A console handle is pointer sized. Without this ctypes would hand back a
    # truncated 32-bit int and every call on it would fail.
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]
    # Drop our own console first, or AttachConsole refuses with
    # ERROR_ACCESS_DENIED. This is why the work lives in a child process.
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        print(f"AttachConsole({pid}) failed, error {kernel32.GetLastError()}", file=sys.stderr)
        return 1

    handle = kernel32.CreateFileW(
        "CONIN$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if handle in (0, INVALID_HANDLE_VALUE, None):
        print(f"could not open CONIN$, error {kernel32.GetLastError()}", file=sys.stderr)
        return 1

    buf = (_INPUT_RECORD * len(records))(*records)
    written = wintypes.DWORD(0)
    kernel32.WriteConsoleInputW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    ok = kernel32.WriteConsoleInputW(
        handle, ctypes.byref(buf), len(records), ctypes.byref(written)
    )
    if not ok:
        print(f"WriteConsoleInputW failed, error {kernel32.GetLastError()}", file=sys.stderr)
        return 1
    if written.value != len(records):
        print(f"wrote {written.value} of {len(records)} key events", file=sys.stderr)
        return 1

    print(f"{written.value} key events written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1:]))
