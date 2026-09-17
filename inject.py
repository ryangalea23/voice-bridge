"""Inject text prompts into the Claude Code voice session window.

claude-voice.ps1 writes its console HWND to ~/.claude/voice-session.hwnd.
inject.py reads that handle directly — no title matching needed.

Uses AttachThreadInput before SetForegroundWindow to reliably steal focus
on Windows 10/11, which blocks naive SetForegroundWindow from background processes.
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

log = logging.getLogger(__name__)

HWND_FILE = os.path.expanduser(r"~\.claude\voice-session.hwnd")

user32 = ctypes.windll.user32


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
        win32gui.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, target_thread, False)


def inject_prompt(text: str) -> bool:
    """Paste text + Enter into the active claude-voice window."""
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
