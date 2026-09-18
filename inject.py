"""Inject text prompts into the Claude Code voice session.

Two ways in, tried in that order:

1. The console input buffer (console_inject.py). The text is written straight
   into the session's console as key events. No focus, no clipboard, and it
   works when the console window is hidden, which is what happens under Tabby,
   Windows Terminal and VS Code.
2. The old clipboard-and-focus path in this file: put the text on the clipboard,
   pull the saved window to the front with AttachThreadInput plus
   SetForegroundWindow, and send Ctrl+V and Enter. Kept as a fallback for cases
   the first path cannot reach.

INJECT_METHOD picks between them: auto (default, console then keys), console, or
keys. claude-voice.ps1 writes the session's pid and window handle under
~/.claude/ for both paths to read.
"""
import ctypes
import logging
import os
import time

import win32api
import win32clipboard
import win32con
import win32gui
import win32process

import console_inject

log = logging.getLogger(__name__)

HWND_FILE = console_inject.HWND_FILE
# claude-voice.ps1 writes this when it cannot find a window that can receive
# typed text, e.g. a ConPTY terminal with no visible ancestor window. The text
# is a spoken sentence, so the bridge can read it out instead of the generic
# "session not active".
HWND_ERROR_FILE = os.path.expanduser(r"~\.claude\voice-session.hwnd.error")
# "console" (a classic conhost window) or "terminal" (the terminal app's own
# window, used when the console window is a hidden pseudo-console).
HWND_KIND_FILE = os.path.expanduser(r"~\.claude\voice-session.hwnd.kind")

user32 = ctypes.windll.user32


def session_error() -> str | None:
    """Why the launcher could not save a usable window handle, or None."""
    try:
        with open(HWND_ERROR_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def session_hwnd_kind() -> str:
    try:
        with open(HWND_KIND_FILE, encoding="utf-8") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _process_name(pid: int) -> str:
    try:
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
        try:
            return os.path.basename(win32process.GetModuleFileNameEx(handle, 0))
        finally:
            handle.Close()
    except Exception:
        return "unknown"


def _get_session_hwnd() -> int | None:
    try:
        with open(HWND_FILE) as f:
            val = f.read().strip()
        hwnd = int(val)
        if win32gui.IsWindow(hwnd):
            return hwnd
        log.error("Stored HWND %s is no longer valid", hwnd)
        return None
    except FileNotFoundError:
        log.error("No voice session active — run claude-voice to start one")
        return None
    except Exception as exc:
        log.error("Could not read HWND file: %s", exc)
        return None


def _force_foreground(hwnd: int) -> None:
    """Reliably bring hwnd to front using AttachThreadInput."""
    current_thread = win32api.GetCurrentThreadId()
    target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)

    attached = False
    if current_thread != target_thread:
        attached = bool(user32.AttachThreadInput(current_thread, target_thread, True))

    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(hwnd)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            # The usual cause is a hidden pseudo-console window under a terminal
            # app. Say enough here that the next failure does not need a repro.
            pid = 0
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pass
            log.error(
                "SetForegroundWindow failed for HWND %s (visible=%s, kind=%s, owner=%s, title=%r)",
                hwnd,
                bool(win32gui.IsWindowVisible(hwnd)),
                session_hwnd_kind(),
                _process_name(pid) if pid else "unknown",
                win32gui.GetWindowText(hwnd),
            )
            raise
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, target_thread, False)


def _send_escape_keys() -> bool:
    """Press Escape by focusing the saved window and sending the key."""
    hwnd = _get_session_hwnd()
    if not hwnd:
        return False

    try:
        _force_foreground(hwnd)
        time.sleep(0.1)
        win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
        win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)
        log.info("Sent Escape to HWND %s", hwnd)
        return True
    except Exception as exc:
        log.error("Escape error: %s", exc)
        return False


def _inject_prompt_keys(text: str) -> bool:
    """Paste text + Enter by focusing the saved window and sending the keys."""
    hwnd = _get_session_hwnd()
    if not hwnd:
        return False

    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        win32clipboard.CloseClipboard()
    except Exception as exc:
        log.error("Clipboard error: %s", exc)
        return False

    try:
        _force_foreground(hwnd)
        time.sleep(0.1)

        # Ctrl+V
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        win32api.keybd_event(ord("V"), 0, 0, 0)
        win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
        win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)

        # Enter
        win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
        win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)

        log.info("Injected %d chars into HWND %s: %s...", len(text), hwnd, text[:60])
        return True
    except Exception as exc:
        log.error("Injection error: %s", exc)
        return False


def inject_method() -> str:
    """Which path to use: auto (default), console, or keys."""
    raw = (os.environ.get("INJECT_METHOD") or "").strip().lower()
    if raw in ("auto", "console", "keys"):
        return raw
    if raw:
        log.warning("Unknown INJECT_METHOD %r, using auto", raw)
    return "auto"


def _try_console(action, label: str, method: str) -> bool | None:
    """Run the console path. None means the caller should fall back."""
    if method == "keys":
        return None
    try:
        ok, reason = action()
    except Exception as exc:
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    if ok:
        log.info("%s via the console input buffer (%s)", label, reason)
        return True
    log.warning("Console input buffer failed for %s: %s", label, reason)
    if method == "console":
        return False
    return None


def send_escape() -> bool:
    """Press Escape in the voice session to interrupt the turn."""
    method = inject_method()
    result = _try_console(console_inject.send_escape, "Escape", method)
    if result is not None:
        return result
    log.info("Falling back to the focus-and-keys path for Escape")
    return _send_escape_keys()


def inject_prompt(text: str) -> bool:
    """Type text + Enter into the voice session."""
    method = inject_method()
    result = _try_console(lambda: console_inject.inject_text(text), "Prompt", method)
    if result is not None:
        return result
    log.info("Falling back to the focus-and-keys path for the prompt")
    return _inject_prompt_keys(text)
