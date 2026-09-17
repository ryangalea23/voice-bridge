"""Desk microphone -> Claude Code, always listening.

The microphone is on by default and streams continuously to Deepgram. There
is no push-to-talk key. Three independent ways to mute/unmute:
  - Spoken command: "stop listening" / "mic off" to mute, "start listening" /
    "mic on" to unmute (only reachable while already listening — see below).
  - Hotkey toggle: press (not hold) the DESK_MIC_HOTKEY key, default Right Ctrl.
  - Console key: press 'm' in this window (no Enter needed).

SAFETY: the target Claude Code session runs with permissions skipped, so
anything this microphone hears and transcribes with enough confidence gets
typed in and executed. This script is not clever about deciding what was
"meant" for it — see the confidence/length filters below — so mute it
whenever you don't want it listening. A muted mic cannot hear "start
listening", which is why the startup banner spells out the hotkey and
console key.

Reuses:
  - stt.py's DeepgramSTT, extended (not duplicated) with encoding/sample_rate/
    channels params (16kHz linear16 mono here vs. the phone bridge's 8kHz
    mulaw) and an optional on_utterance_full(text, confidence) callback, used
    here to apply the confidence filter below. bridge.py's call site is
    unaffected — it still passes only on_utterance.
  - inject.py's inject_prompt() and its HWND validity check pattern.
  - voice_text.strip_fillers(), the filler-word stripper factored out of
    bridge.py into its own module so both files share one regex instead of
    two copies that could drift.
  - bridge.py's _handle_voice_command pattern (small compiled regexes,
    checked in order, consumed and not injected as text) for the mute/unmute
    spoken commands below. Not imported — bridge.py's version is phone-call
    specific (switch/check/list/repeat) and pulls in the whole FastAPI/Twilio
    app; only the pattern shape is reused, not the code.
"""
import asyncio
import logging
import os
import re
import sys
import threading
from pathlib import Path

import msvcrt
import numpy as np
import psutil
import sounddevice as sd
import win32gui
import win32process
from dotenv import load_dotenv

from inject import HWND_FILE, inject_prompt
from stt import DeepgramSTT
from voice_text import strip_fillers

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("desk_mic")

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
SAMPLE_RATE = 16000
CHANNELS = 1

# Mute/unmute spoken commands — same shape as bridge.py's _CMD_* patterns:
# small anchored regexes, checked before anything is injected.
_CMD_MIC_OFF = re.compile(r"^\s*(?:stop listening|mic off)\.?\s*$", re.IGNORECASE)
_CMD_MIC_ON = re.compile(r"^\s*(?:start listening|mic on)\.?\s*$", re.IGNORECASE)

# Dumb, honest guards against "it will hear everything else" — length and
# confidence only. No keyword/intent guessing about what was meant for us.
MIN_CHARS = int(os.environ.get("DESK_MIC_MIN_CHARS", "4"))
MIN_CONFIDENCE = float(os.environ.get("DESK_MIC_MIN_CONFIDENCE", "0.6"))

# Push-to-talk is gone; this is now a toggle hotkey. Configurable via env var.
HOTKEY_NAME = os.environ.get("DESK_MIC_HOTKEY", "right ctrl")

# TTS playback detection (problem: it will hear itself). speak.py (fired by
# the Stop hook) synthesizes with edge-tts and plays through ffplay.exe;
# the Stop hook only launches it at all when this marker file exists.
# ffplay.exe exists as a live process for exactly the duration of playback,
# which is a tighter signal than the marker (the marker just means TTS is
# enabled, not that it's speaking right now) — so: marker present is a fast
# "could it be talking at all" check, and the process scan is the actual
# "is it talking right now" check. See stop-hook.py / speak.py.
TTS_MARKER = Path.home() / ".claude" / "scripts" / ".tts-enabled"


def _is_tts_playing() -> bool:
    if not TTS_MARKER.exists():
        return False
    try:
        for p in psutil.process_iter(["name"]):
            name = p.info.get("name")
            if name and name.lower() == "ffplay.exe":
                return True
        return False
    except Exception as exc:
        # Fail safe: if we can't tell whether it's speaking, assume it is and
        # mute, rather than risk transcribing Claude's own voice back into
        # itself. This trades a possible missed real utterance for avoiding
        # a feedback loop on a permissions-skipped session.
        log.warning("TTS-playback detection failed (%s) — muting capture as a precaution", exc)
        return True


def _resolve_hotkey_vk(name: str) -> int:
    """Map a small set of friendly names to virtual-key codes."""
    import win32con

    names = {
        "right ctrl": win32con.VK_RCONTROL,
        "left ctrl": win32con.VK_LCONTROL,
        "right shift": win32con.VK_RSHIFT,
        "left shift": win32con.VK_LSHIFT,
        "right alt": win32con.VK_RMENU,
        "left alt": win32con.VK_LMENU,
        "caps lock": win32con.VK_CAPITAL,
        "scroll lock": win32con.VK_SCROLL,
        "f13": win32con.VK_F13,
        "f14": win32con.VK_F14,
        "f15": win32con.VK_F15,
    }
    key = names.get(name.strip().lower())
    if key is None:
        raise ValueError(
            f"Unknown DESK_MIC_HOTKEY value {name!r}. Known: {sorted(names)}"
        )
    return key


HOTKEY_VK = _resolve_hotkey_vk(HOTKEY_NAME)


# ── HWND validity check (same pattern as inject.py's _get_session_hwnd) ────────

def _check_target_window() -> int:
    """Verify the recorded session HWND exists and is a live window.

    Exits the process with a clear error if the file is missing or the
    window is dead — mirrors inject.py's _get_session_hwnd(), but this
    check must fail loudly at startup instead of silently at injection time.
    """
    try:
        with open(HWND_FILE) as f:
            hwnd = int(f.read().strip())
    except FileNotFoundError:
        log.error(
            "No voice session recorded at %s. Start one with claude-voice first.",
            HWND_FILE,
        )
        sys.exit(1)
    except Exception as exc:
        log.error("Could not read %s: %s", HWND_FILE, exc)
        sys.exit(1)

    if not win32gui.IsWindow(hwnd):
        log.error("Stored HWND %s in %s is no longer a valid window.", hwnd, HWND_FILE)
        sys.exit(1)

    title = win32gui.GetWindowText(hwnd)
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = psutil.Process(pid).name()
    except Exception:
        proc_name = "<unknown process>"

    log.info("Target window: hwnd=%s process=%s title=%r", hwnd, proc_name, title)
    return hwnd


# ── Mic on/off state, shared across threads ─────────────────────────────────

class MicState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.listening = True
        self.tts_playing = False

    def set_listening(self, value: bool, source: str) -> bool:
        """Returns True if the state actually changed."""
        with self._lock:
            if self.listening == value:
                return False
            self.listening = value
        word = "LISTENING" if value else "MUTED"
        print(f"\n>>> MIC {word} (via {source})\n", flush=True)
        log.warning("Mic state -> %s (via %s)", word, source)
        return True

    def toggle(self, source: str) -> None:
        self.set_listening(not self.listening, source)


# ── Key watchers ─────────────────────────────────────────────────────────────

def _key_is_down(vk: int) -> bool:
    import win32api

    return bool(win32api.GetAsyncKeyState(vk) & 0x8000)


class HotkeyToggle:
    """Fires on_press once per physical key-down (not on hold-repeat)."""

    def __init__(self, vk: int, on_press, poll_interval: float = 0.02):
        self._vk = vk
        self._on_press = on_press
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _run(self) -> None:
        was_down = False
        while not self._stop.is_set():
            down = _key_is_down(self._vk)
            if down and not was_down:
                was_down = True
                self._on_press()
            elif not down and was_down:
                was_down = False
            self._stop.wait(self._poll_interval)


class ConsoleKeyWatcher:
    """Polls the console for a keypress (no Enter needed) via msvcrt."""

    def __init__(self, on_key, poll_interval: float = 0.05):
        self._on_key = on_key
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _run(self) -> None:
        while not self._stop.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                try:
                    self._on_key(ch.decode(errors="ignore"))
                except Exception as exc:
                    log.debug("Console key handler error: %s", exc)
            self._stop.wait(self._poll_interval)


# ── TTS playback monitor ─────────────────────────────────────────────────────

class TTSMonitor:
    def __init__(self, state: MicState, poll_interval: float = 0.15):
        self._state = state
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _run(self) -> None:
        was_playing = False
        while not self._stop.is_set():
            playing = _is_tts_playing()
            if playing != was_playing:
                was_playing = playing
                if playing:
                    print(">>> TTS playing — pausing capture so it doesn't hear itself", flush=True)
                else:
                    print(">>> TTS finished — capture resumed", flush=True)
            self._state.tts_playing = playing
            self._stop.wait(self._poll_interval)


# ── Microphone capture + Deepgram plumbing ──────────────────────────────────

class DeskMicSession:
    def __init__(self, loop: asyncio.AbstractEventLoop, state: MicState):
        self._loop = loop
        self._state = state
        self._stt: DeepgramSTT | None = None
        self._stream: sd.InputStream | None = None

    async def _on_utterance_full(self, text: str, confidence: float) -> None:
        if not self._state.listening:
            log.info("Muted — dropping stray utterance: %r", text)
            return

        if confidence < MIN_CONFIDENCE:
            log.info("Dropped (confidence %.2f < %.2f): %r", confidence, MIN_CONFIDENCE, text)
            return

        text = strip_fillers(text)
        if len(text) < MIN_CHARS:
            log.info("Dropped (too short, %d < %d chars): %r", len(text), MIN_CHARS, text)
            return

        print(f"Final: {text!r} (confidence={confidence:.2f})", flush=True)

        if _CMD_MIC_OFF.match(text):
            self._state.set_listening(False, "voice command")
            return
        if _CMD_MIC_ON.match(text):
            # Unreachable in practice — a muted mic isn't sent to Deepgram —
            # but harmless to keep as a no-op safety net.
            self._state.set_listening(True, "voice command")
            return

        ok = inject_prompt(text)
        print(f"Injection {'succeeded' if ok else 'FAILED'}", flush=True)

    def start(self) -> None:
        self._stt = DeepgramSTT(
            on_utterance_full=self._on_utterance_full,
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
        )
        fut = asyncio.run_coroutine_threadsafe(self._stt.start(DEEPGRAM_API_KEY), self._loop)
        fut.result(timeout=10)

        def _callback(indata, frames, time_info, status):
            if status:
                log.warning("Audio status: %s", status)
            # Gate at the point of sending to Deepgram — muted or TTS-playing
            # audio is captured from the device (cheap) but never leaves this
            # process. Capture stays open continuously; only the "send"
            # decision toggles, so mute/unmute is instant with no stream
            # restart.
            if not self._state.listening or self._state.tts_playing:
                return
            pcm16 = (indata[:, 0] * 32767).astype(np.int16).tobytes()
            asyncio.run_coroutine_threadsafe(self._stt.send(pcm16), self._loop)

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=_callback,
        )
        self._stream.start()
        log.info("Continuous capture started (%d Hz, %d ch, linear16)", SAMPLE_RATE, CHANNELS)

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._stt:
            fut = asyncio.run_coroutine_threadsafe(self._stt.finish(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception as exc:
                log.warning("Deepgram finish error: %s", exc)
            self._stt = None


def main() -> None:
    _check_target_window()

    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0]
        if default_in is None or default_in < 0:
            log.error("No default input device found. Plug in a microphone and retry.")
            sys.exit(1)
        log.info("Default input device: %s", devices[default_in]["name"])
    except Exception as exc:
        log.error("Could not query audio devices: %s", exc)
        sys.exit(1)

    state = MicState()

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    session = DeskMicSession(loop, state)
    session.start()

    def _on_hotkey():
        state.toggle(f"hotkey ({HOTKEY_NAME})")

    def _on_console_key(ch: str):
        if ch.lower() == "m":
            state.toggle("console key 'm'")

    hotkey = HotkeyToggle(HOTKEY_VK, _on_hotkey)
    hotkey.start()

    console_keys = ConsoleKeyWatcher(_on_console_key)
    console_keys.start()

    tts_monitor = TTSMonitor(state)
    tts_monitor.start()

    print("=" * 78)
    print("DESK MIC IS LIVE. It listens continuously and anything it hears with")
    print("enough length and confidence is typed into a Claude Code session that")
    print("runs commands WITHOUT asking for permission. Mute it when you don't")
    print("want it listening.")
    print("-" * 78)
    print(f"  Say 'stop listening' or 'mic off' to mute.")
    print(f"  Say 'start listening' or 'mic on' to unmute — but note: a muted mic")
    print(f"  cannot hear this, since muted audio is never sent to Deepgram.")
    print(f"  To turn it back ON while muted, use one of:")
    print(f"    - press {HOTKEY_NAME} (toggle)")
    print(f"    - press 'm' in this console window (toggle)")
    print(f"  Filters: drop transcripts under {MIN_CHARS} chars or below")
    print(f"  {MIN_CONFIDENCE:.2f} confidence (DESK_MIC_MIN_CHARS / DESK_MIC_MIN_CONFIDENCE).")
    print("=" * 78)
    log.info("Ready. Listening.")

    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        hotkey.stop()
        console_keys.stop()
        tts_monitor.stop()
        session.stop()
        loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    main()
