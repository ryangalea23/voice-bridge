"""Twilio voice bridge — routes phone calls to Claude Code CLI and back.

Routes:
  POST /twilio/voice          — Twilio webhook; returns TwiML to open Media Stream
  WS   /twilio/stream         — Twilio Media Streams WebSocket (inbound + outbound)
  POST /session/register      — claude-voice.ps1 calls this on launch
  POST /session/deregister    — claude-voice.ps1 calls this on exit
  POST /hook/assistant-text   — Stop hook endpoint (localhost-only)
  POST /hook/tool-use         — PreToolUse hook endpoint (localhost-only)
  GET  /health                — health check

Session model:
  One "active" session gets full voice output.
  Background sessions get a one-sentence notification when they finish a turn.
  Voice commands ("switch to terminal 2", "check terminal 3") are intercepted
  in the STT callback before being injected into Claude Code.
"""
import asyncio
import base64
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from twilio.request_validator import RequestValidator

from inject import inject_prompt, send_escape
import readback
from stt import DeepgramSTT
from tts import text_to_mulaw_chunks
from voice_settings import load_settings, log_settings
from voice_text import is_stop_command, strip_fillers
import typing_sound

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("bridge")

TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
VALIDATE_TWILIO_SIG = os.environ.get("VALIDATE_TWILIO", "true").lower() == "true"

# Who is allowed to drive this machine by phone. Comma-separated numbers, any
# format - only the digits are compared. This FAILS CLOSED: an empty or missing
# ALLOWED_CALLERS rejects every call. A bridge that types whatever it hears
# into a terminal must not be open to whoever dials the number.
_RAW_CALLERS = [n.strip() for n in os.environ.get("ALLOWED_CALLERS", "").split(",") if n.strip()]
ALLOWED_CALLERS = {"".join(c for c in n if c.isdigit()) for n in _RAW_CALLERS}
# Shared secret for /session/register and /session/deregister. The public tunnel
# exposes every route, not just the Twilio ones, so these need their own gate.
# Also fails closed.
SESSION_TOKEN = os.environ.get("SESSION_TOKEN", "").strip()

# One-time tickets that bind an accepted call to the WebSocket Twilio opens for
# it. Without this the caller allowlist is decorative: /twilio/voice does the
# identity check, but the media stream is a separate connection, so anyone who
# knew the tunnel URL could open it directly and skip the check entirely.
STREAM_TICKET_TTL = 120.0          # seconds between the TwiML reply and the socket
_stream_tickets: dict[str, float] = {}


def _mint_stream_ticket() -> str:
    now = time.monotonic()
    for t, exp in [(t, e) for t, e in _stream_tickets.items() if e <= now]:
        _stream_tickets.pop(t, None)
    ticket = secrets.token_urlsafe(24)
    _stream_tickets[ticket] = now + STREAM_TICKET_TTL
    return ticket


def _redeem_stream_ticket(ticket: str) -> bool:
    """Single use. A ticket is valid once, and only inside its TTL."""
    if not ticket:
        return False
    expires = _stream_tickets.pop(ticket, None)
    return expires is not None and expires > time.monotonic()


def _check_config() -> None:
    """Fail loudly at startup, not silently on the first call.

    Twilio always sends From in E.164 (+ then country code then number), so an
    entry written without the country code will never match. Rather than guess
    what the user meant, say so plainly and name the bad entry.
    """
    if not ALLOWED_CALLERS:
        log.warning(
            "ALLOWED_CALLERS is empty - EVERY call will be rejected. "
            "Set it in .env, e.g. ALLOWED_CALLERS=+15551112222"
        )
    for raw in _RAW_CALLERS:
        digits = "".join(c for c in raw if c.isdigit())
        if not raw.lstrip().startswith("+") or not (8 <= len(digits) <= 15):
            log.warning(
                "ALLOWED_CALLERS entry %r does not look like E.164. Twilio sends "
                "numbers as +<country><number>, so this entry will never match. "
                "Write it as +15551112222, not 5551112222.",
                raw,
            )
    if not SESSION_TOKEN:
        log.warning(
            "SESSION_TOKEN is empty - /session/register and /session/deregister "
            "will reject everything, so claude-voice cannot register a window."
        )


_check_config()

# Read-back, stop words and Deepgram keyterms. Read once here, not per call.
SETTINGS = load_settings()
log_settings(SETTINGS)
HEARTBEAT_INTERVAL = 12.0
# Typing sound replaces the old spoken filler phrases. Set TYPING_SOUND=0 for
# pure silence while a turn runs.
TYPING_SOUND = os.environ.get("TYPING_SOUND", "1") != "0"

_ACK_PHRASES = [
    "On it.",
    "Got it.",
    "Sure.",
    "Yep.",
    "On it, give me a sec.",
    "Right.",
    "Let me check.",
    "Yeah, one sec.",
    "Sure thing.",
    "On it.",
]

app = FastAPI()

# ── Speech channel state ───────────────────────────────────────────────────────

_tool_version: int = 0
_content_version: int = 0
_last_speech_time: float = 0.0
_heartbeat_task: Optional[asyncio.Task] = None
_typing_task: Optional[asyncio.Task] = None
_ack_idx: int = 0
_tool_busy: int = 0
_turn_active: bool = False

# ── Session registry ───────────────────────────────────────────────────────────

# num -> {hwnd, last_text, last_time, transcript_path}
_sessions: dict = {}
_active_session_num: int = 0   # 0 = unset, first stop hook wins

# ── Transcript watcher state ───────────────────────────────────────────────────

_transcript_path: str = ""
_watcher_task: Optional[asyncio.Task] = None
_watcher_last_text: str = ""
_watcher_spoke_this_turn: bool = False

# ── Per-call state ─────────────────────────────────────────────────────────────

class _CallState:
    ws: Optional[WebSocket] = None
    stream_sid: Optional[str] = None
    active: bool = False
    speaking: bool = False
    interrupted: bool = False
    speak_since: float = 0.0
    outbound_q: asyncio.Queue = None
    stop_hint_given: bool = False  # "say stop to cancel" is spoken once per call

    def reset(self) -> None:
        self.ws = None
        self.stream_sid = None
        self.active = False
        self.speaking = False
        self.interrupted = False
        self.stop_hint_given = False

_call = _CallState()

# ── Markdown cleaning ──────────────────────────────────────────────────────────

def _clean_for_tts(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    result_lines = []
    for line in text.splitlines():
        s = line.strip()
        if set(s) <= set("-|: "):
            continue
        elif s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|") if c.strip()]
            result_lines.append(", ".join(cells))
        else:
            result_lines.append(line)
    text = "\n".join(result_lines)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ── TwiML helpers ──────────────────────────────────────────────────────────────

def _connect_twiml(ws_url: str, ticket: str = "") -> str:
    """Twilio drops the query string on the Media Stream URL, so the ticket
    travels as a <Parameter>, which arrives in the start frame's
    customParameters. The query string is still set for anything that does
    preserve it."""
    param = f'<Parameter name="ticket" value="{ticket}"/>' if ticket else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="{ws_url}">{param}</Stream></Connect></Response>'
    )
def _form_params(raw: bytes) -> dict:
    """Parse a Twilio application/x-www-form-urlencoded body."""
    from urllib.parse import unquote_plus
    params = {}
    for pair in raw.decode().split("&"):
        if "=" in pair:
            k, _, v = pair.partition("=")
            params[unquote_plus(k)] = unquote_plus(v)
    return params


def _caller_allowed(from_number: str) -> bool:
    """Twilio sends From in E.164. Compare on digits so +1 555 111 2222,
    +15551112222 and 555-111-2222 all match the same entry."""
    if not ALLOWED_CALLERS:
        return False
    return "".join(c for c in from_number if c.isdigit()) in ALLOWED_CALLERS


def _reject_twiml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Say>This number is not authorized.</Say><Hangup/></Response>"
    )


def _session_authorized(request: Request) -> bool:
    if not SESSION_TOKEN:
        return False
    return hmac.compare_digest(
        request.headers.get("x-bridge-token", ""), SESSION_TOKEN
    )



# ── /twilio/voice — webhook ────────────────────────────────────────────────────

@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    raw = await request.body()
    params = _form_params(raw)

    if VALIDATE_TWILIO_SIG:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        sig = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(str(request.url), params, sig):
            log.warning("Rejected request with invalid Twilio signature")
            return Response(status_code=403)

    # A valid Twilio signature only proves Twilio sent the webhook. It says
    # nothing about who dialled, so check the caller before handing them a
    # live terminal.
    caller = params.get("From", "")
    if not _caller_allowed(caller):
        log.warning("Rejected call from %s (not in ALLOWED_CALLERS)", caller or "<unknown>")
        return Response(content=_reject_twiml(), media_type="application/xml")
    log.info("Accepted call from %s", caller)

    host = request.headers.get("host", "localhost:8000")
    # Bind this accepted call to the socket Twilio is about to open.
    ticket = _mint_stream_ticket()
    ws_url = f"wss://{host}/twilio/stream?ticket={ticket}"
    return Response(content=_connect_twiml(ws_url, ticket), media_type="application/xml")

# ── Outbound audio sender ──────────────────────────────────────────────────────

async def _audio_sender(ws: WebSocket) -> None:
    while _call.active:
        try:
            chunk: Optional[bytes] = await asyncio.wait_for(_call.outbound_q.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        if chunk is None:
            break
        if _call.stream_sid:
            await ws.send_text(json.dumps({
                "event": "media",
                "streamSid": _call.stream_sid,
                "media": {"payload": base64.b64encode(chunk).decode()},
            }))

# ── Speech channels ────────────────────────────────────────────────────────────

async def _speak_tool(text: str) -> None:
    """Low-priority: progress phrases and heartbeat. Skipped if content is speaking."""
    global _tool_version, _last_speech_time
    if not _call.active or _call.speaking:
        return
    global _tool_busy
    _tool_version += 1
    my_version = _tool_version
    _last_speech_time = asyncio.get_event_loop().time()
    _tool_busy += 1
    try:
        async for chunk in text_to_mulaw_chunks(text):
            if _tool_version != my_version or _call.interrupted or not _call.active:
                return
            await _call.outbound_q.put(chunk)
    except Exception as exc:
        log.error("Tool speak error: %s", exc)
    finally:
        _tool_busy -= 1


async def _speak_content(text: str) -> None:
    """High-priority: Claude's actual text. Cancels any tool phrase, always plays."""
    global _content_version, _tool_version, _last_speech_time
    _content_version += 1
    my_version = _content_version

    _tool_version += 1
    while not _call.outbound_q.empty():
        try:
            _call.outbound_q.get_nowait()
        except asyncio.QueueEmpty:
            break

    if not _call.active:
        return

    _last_speech_time = asyncio.get_event_loop().time()
    _call.speaking = True
    _call.interrupted = False
    _call.speak_since = _last_speech_time
    try:
        async for chunk in text_to_mulaw_chunks(text):
            if _content_version != my_version or _call.interrupted or not _call.active:
                log.info("Content speak cancelled (v%d, mine=%d)", _content_version, my_version)
                return
            await _call.outbound_q.put(chunk)
    except Exception as exc:
        log.error("Content speak error: %s", exc)
    finally:
        if _content_version == my_version:
            _call.speaking = False

# ── Rich tool phrases ──────────────────────────────────────────────────────────

def _build_tool_phrase(tool: str, inp: dict) -> Optional[str]:
    t = tool.lower()
    if "read" in t:
        path = inp.get("file_path", inp.get("path", ""))
        name = os.path.basename(path) if path else ""
        return f"Reading {name}..." if name else "Reading..."
    if "write" in t:
        path = inp.get("file_path", inp.get("path", ""))
        name = os.path.basename(path) if path else ""
        return f"Writing {name}..." if name else "Writing file..."
    if "edit" in t:
        path = inp.get("file_path", inp.get("path", ""))
        name = os.path.basename(path) if path else ""
        return f"Editing {name}..." if name else "Editing..."
    if "glob" in t:
        pattern = inp.get("pattern", "")
        return f"Finding {pattern}..." if pattern else "Searching files..."
    if "grep" in t or ("search" in t and "web" not in t):
        pattern = inp.get("pattern", "")
        short = pattern[:25] if pattern else ""
        return f"Searching for {short}..." if short else "Searching code..."
    if "bash" in t or "powershell" in t:
        cmd = inp.get("command", "").strip()
        first = cmd.split()[0][:20] if cmd else ""
        return f"Running {first}..." if first else "Running command..."
    if "web_search" in t:
        query = inp.get("query", "")
        short = query[:25] if query else ""
        return f"Searching the web for {short}..." if short else "Searching the web..."
    if "web_fetch" in t or "fetch" in t:
        return "Fetching page..."
    if "agent" in t:
        return "Spinning up agent..."
    if "notebook" in t:
        return "Editing notebook..."
    return None

# ── Typing sound ───────────────────────────────────────────────────────────────

async def _typing_loop() -> None:
    """Play keyboard typing while a turn is in flight.

    Ducks under anything being spoken, and paces itself at real time so it does
    not flood the Twilio buffer. Cancelled when the turn ends, so silence is the
    resting state.
    """
    gen = typing_sound.chunks(typing_sound.random_offset())
    try:
        while _call.active and _turn_active:
            if _call.speaking or _call.interrupted or _tool_busy > 0:
                await asyncio.sleep(0.1)
                continue
            await _call.outbound_q.put(next(gen))
            await asyncio.sleep(0.19)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.error("Typing loop error: %s", exc)


def _start_typing() -> None:
    global _typing_task, _turn_active
    _turn_active = True
    # A new turn means the user's barge-in has been handled. This flag used to be
    # cleared by the spoken acknowledgement, which no longer exists, so clearing
    # it here is what keeps the typing loop from silently skipping every chunk.
    _call.interrupted = False
    if not TYPING_SOUND:
        return
    if _typing_task and not _typing_task.done():
        return
    _typing_task = asyncio.create_task(_typing_loop())
    log.info("Typing sound started")


def _stop_typing() -> None:
    global _typing_task, _turn_active
    _turn_active = False
    if _typing_task and not _typing_task.done():
        _typing_task.cancel()
        log.info("Typing sound stopped")
    _typing_task = None


# ── Heartbeat ──────────────────────────────────────────────────────────────────

async def _heartbeat() -> None:
    """Watchdog only. The old spoken filler phrases were removed on request:
    while a turn runs you hear typing, and when nothing runs you hear silence."""
    while _call.active:
        await asyncio.sleep(3.0)
        if not _call.active:
            break
        if _turn_active and TYPING_SOUND and (not _typing_task or _typing_task.done()):
            log.info("Typing task died mid-turn — restarting")
            _start_typing()

# ── Session management ─────────────────────────────────────────────────────────

def _update_active_hwnd(hwnd: str) -> None:
    hwnd_file = os.path.expanduser(r"~\.claude\voice-session.hwnd")
    try:
        with open(hwnd_file, "w") as f:
            f.write(str(hwnd))
        log.info("Active HWND updated to %s", hwnd)
    except Exception as exc:
        log.warning("Could not update HWND file: %s", exc)


async def _switch_to_session(num: int) -> None:
    global _active_session_num, _transcript_path
    if num not in _sessions:
        await _speak_content(f"No terminal {num} registered.")
        return
    _active_session_num = num
    hwnd = _sessions[num].get("hwnd", "")
    if hwnd:
        _update_active_hwnd(hwnd)
    tp = _sessions[num].get("transcript_path", "")
    if tp:
        _transcript_path = tp
    log.info("Switched active session to terminal %d", num)
    await _speak_content(f"Switched to terminal {num}.")


async def _check_session(num: int) -> None:
    if num not in _sessions:
        await _speak_content(f"No terminal {num} registered.")
        return
    last = _sessions[num].get("last_text", "")
    if last:
        first = last.split(".")[0][:150].strip()
        await _speak_content(f"Terminal {num} last said: {first}.")
    else:
        await _speak_content(f"Terminal {num} hasn't spoken yet.")


async def _list_sessions_voice() -> None:
    if not _sessions:
        await _speak_content("No terminals registered.")
        return
    parts = []
    for num in sorted(_sessions.keys()):
        marker = " active" if num == _active_session_num else ""
        parts.append(f"terminal {num}{marker}")
    await _speak_content("Running: " + ", ".join(parts) + ".")

# ── Voice command interception ─────────────────────────────────────────────────

_CMD_SWITCH = re.compile(
    r'\b(?:switch|go|move|jump)\s+to\s+(?:terminal|session)\s+(\d+)\b', re.IGNORECASE
)
_CMD_CHECK = re.compile(
    r'\b(?:check|what.?s|status\s+of)\s+(?:terminal|session)\s+(\d+)\b', re.IGNORECASE
)
_CMD_LIST = re.compile(
    r'\b(?:list|show)\s+(?:my\s+)?(?:terminals|sessions)\b', re.IGNORECASE
)
_CMD_REPEAT = re.compile(
    r'^\s*(?:repeat|say that again|what did you say|can you repeat that)\??\s*$', re.IGNORECASE
)


async def _handle_voice_command(text: str) -> bool:
    """Returns True if text was a bridge control command (don't inject into Claude)."""
    if _CMD_REPEAT.match(text):
        if _watcher_last_text:
            await _speak_content(_watcher_last_text)
        else:
            await _speak_content("Nothing to repeat yet.")
        return True
    m = _CMD_SWITCH.search(text)
    if m:
        await _switch_to_session(int(m.group(1)))
        return True
    m = _CMD_CHECK.search(text)
    if m:
        await _check_session(int(m.group(1)))
        return True
    if _CMD_LIST.search(text):
        await _list_sessions_voice()
        return True
    return False

# ── Transcript watcher ─────────────────────────────────────────────────────────

async def _watch_transcript(path: str, start_pos: int) -> None:
    global _watcher_last_text, _watcher_spoke_this_turn
    _watcher_last_text = ""
    pos = start_pos
    buf = ""
    log.info("Watcher started at byte %d: %s", start_pos, path)

    while _call.active:
        await asyncio.sleep(0.15)

        try:
            size = os.path.getsize(path)
        except OSError:
            continue

        if size <= pos:
            continue

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                new_data = f.read()
            pos = size
        except OSError:
            continue

        buf += new_data
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            inner = msg.get("message", msg)
            role = inner.get("role") or msg.get("type")
            if role != "assistant":
                continue

            content = inner.get("content", [])
            if not isinstance(content, list):
                continue

            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            raw = "\n".join(parts).strip()
            if not raw:
                continue

            cleaned = _clean_for_tts(raw)
            if not cleaned:
                continue

            log.info("Watcher speaking %d chars", len(cleaned))
            _watcher_last_text = cleaned
            _watcher_spoke_this_turn = True
            asyncio.create_task(_speak_content(cleaned))

    log.info("Watcher stopped")


def _start_watcher() -> None:
    global _watcher_task
    if not _transcript_path:
        log.info("No transcript path yet — watcher will start after first Stop hook")
        return
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()
    try:
        start_pos = os.path.getsize(_transcript_path)
    except OSError:
        log.warning("Cannot stat transcript %s", _transcript_path)
        return
    _watcher_task = asyncio.create_task(_watch_transcript(_transcript_path, start_pos))

# ── STT utterance callbacks ────────────────────────────────────────────────────

async def _clear_audio() -> None:
    while not _call.outbound_q.empty():
        try:
            _call.outbound_q.get_nowait()
        except asyncio.QueueEmpty:
            break
    if _call.ws and _call.stream_sid:
        try:
            await _call.ws.send_text(json.dumps({
                "event": "clear",
                "streamSid": _call.stream_sid,
            }))
            log.info("Sent Twilio clear — audio stopped")
        except Exception as exc:
            log.warning("Could not send clear: %s", exc)

async def _on_speech_started() -> None:
    if not _call.active:
        return
    elapsed = asyncio.get_event_loop().time() - _call.speak_since
    if elapsed > 3.0:
        log.info("User speaking at elapsed=%.1fs — clearing buffer", elapsed)
        _call.interrupted = True
        await _clear_audio()

def _with_stop_hint(phrase: str) -> str:
    """Add "say stop to cancel" to the first read-back of a call only."""
    if _call.stop_hint_given:
        return phrase
    _call.stop_hint_given = True
    return f"{phrase}. {readback.STOP_HINT}"


async def _haiku_restatement(text: str) -> Optional[str]:
    """Haiku restatement, or None (with the reason logged) so the caller falls
    back to the transcript read-back."""
    timeout_s = SETTINGS.haiku_timeout_ms / 1000
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None, readback.haiku_restate, text, SETTINGS.anthropic_api_key, timeout_s
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        log.warning("Haiku read-back timed out after %dms - using transcript", SETTINGS.haiku_timeout_ms)
    except readback.HaikuError as exc:
        log.warning("Haiku read-back failed (%s) - using transcript", exc)
    except Exception as exc:
        log.warning("Haiku read-back error (%s) - using transcript", exc)
    return None


async def _speak_haiku_readback(text: str, haiku_task: "asyncio.Task[Optional[str]]") -> None:
    restated = await haiku_task
    phrase = f"I heard: {restated}" if restated else readback.transcript_readback(text)
    # If the real answer already started, a late read-back would cut it off.
    if _watcher_spoke_this_turn or not _turn_active:
        log.info("Skipping late read-back, the answer is already out")
        return
    await _speak_content(_with_stop_hint(phrase))


async def _handle_stop() -> None:
    """Interrupt the running turn instead of typing the stop word as a prompt."""
    log.info("Stop command - sending Escape")
    ok = await asyncio.get_event_loop().run_in_executor(None, send_escape)
    _stop_typing()
    if ok:
        await _speak_content("Stopped.")
    else:
        await _speak_content("Session not active. Nothing to stop.")


async def _on_utterance(text: str) -> None:
    global _ack_idx, _watcher_spoke_this_turn
    text = strip_fillers(text)
    if not text:
        return
    log.info("Utterance: %r", text)

    if is_stop_command(text, SETTINGS.stop_words):
        await _handle_stop()
        return

    if await _handle_voice_command(text):
        return

    # Start Haiku before injecting so the two run side by side. Injection never
    # waits on it.
    haiku_task = None
    if SETTINGS.readback == "haiku":
        haiku_task = asyncio.create_task(_haiku_restatement(text))

    ok = await asyncio.get_event_loop().run_in_executor(None, inject_prompt, text)
    if not ok:
        if haiku_task:
            haiku_task.cancel()
        await _speak_content("Session not active. Please open Claude Code with the voice launcher.")
    else:
        _watcher_spoke_this_turn = False
        # Short spoken read-back (or ack) the moment the text is in, then typing
        # underneath until the real answer arrives.
        if SETTINGS.readback == "off":
            ack = _ACK_PHRASES[_ack_idx % len(_ACK_PHRASES)]
            _ack_idx += 1
            asyncio.create_task(_speak_content(ack))
        elif haiku_task:
            asyncio.create_task(_speak_haiku_readback(text, haiku_task))
        else:
            asyncio.create_task(_speak_content(_with_stop_hint(readback.transcript_readback(text))))
        _start_typing()
        _start_watcher()

# ── /twilio/stream — WebSocket ─────────────────────────────────────────────────

@app.websocket("/twilio/stream")
async def twilio_stream(ws: WebSocket) -> None:
    global _heartbeat_task, _last_speech_time

    # This socket feeds straight into the terminal, so it must present the
    # ticket minted when the call passed the caller allowlist. Twilio drops the
    # query string here, so the ticket normally arrives in the start frame
    # instead. Accept the socket, but start nothing until it checks out.
    validated = _redeem_stream_ticket(ws.query_params.get("ticket", ""))

    await ws.accept()
    log.info("Media stream connected (ticket in URL: %s)", validated)

    _call.ws = ws
    _call.active = True
    _call.outbound_q = asyncio.Queue()
    _last_speech_time = asyncio.get_event_loop().time()

    stt = DeepgramSTT(
        on_utterance=_on_utterance,
        on_speech_started=_on_speech_started,
        keyterms=SETTINGS.deepgram_keyterms,
    )
    sender_task = None
    started = False

    async def _begin() -> bool:
        """Start transcription. Only called once the ticket has been redeemed."""
        nonlocal sender_task, started
        global _heartbeat_task
        try:
            await stt.start(DEEPGRAM_API_KEY)
        except Exception as exc:
            log.error("Deepgram failed to start: %s", exc)
            return False
        sender_task = asyncio.create_task(_audio_sender(ws))
        _heartbeat_task = asyncio.create_task(_heartbeat())
        started = True
        return True

    try:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                if not validated:
                    custom = msg["start"].get("customParameters") or {}
                    validated = _redeem_stream_ticket(custom.get("ticket", ""))
                if not validated:
                    log.warning(
                        "Rejected media stream: no valid ticket in the URL or the "
                        "start frame. Someone may be connecting directly."
                    )
                    await ws.close(code=1008)
                    return

                _call.stream_sid = msg["start"]["streamSid"]
                log.info("Stream SID: %s (ticket accepted)", _call.stream_sid)
                if not await _begin():
                    await ws.close(code=1011)
                    return
                asyncio.create_task(_speak_content("Hello, Claude is listening."))

            elif event == "media":
                # Never feed audio anywhere before the ticket is redeemed.
                if not (validated and started):
                    continue
                if msg["media"].get("track", "inbound") == "inbound":
                    audio = base64.b64decode(msg["media"]["payload"])
                    await stt.send(audio)

            elif event == "stop":
                log.info("Stream stop event")
                break

    except WebSocketDisconnect:
        log.info("WebSocket disconnected")
    finally:
        _call.reset()
        if sender_task is not None:
            sender_task.cancel()
        if _heartbeat_task is not None:
            _heartbeat_task.cancel()
        if _watcher_task and not _watcher_task.done():
            _watcher_task.cancel()
        _stop_typing()
        if started:
            await stt.finish()
        log.info("Stream session ended")

# ── /session/register — called by claude-voice.ps1 on launch ──────────────────

@app.post("/session/register")
async def session_register(request: Request) -> dict:
    global _active_session_num
    if not _session_authorized(request):
        log.warning("Rejected /session/register with a bad or missing token")
        return Response(status_code=403)
    body = await request.json()
    num = int(body.get("session_num", 0))
    hwnd = str(body.get("hwnd", ""))
    if not num:
        return {"status": "error", "message": "session_num required"}

    _sessions[num] = {"hwnd": hwnd, "last_text": "", "last_time": 0.0, "transcript_path": ""}
    log.info("Session %d registered (HWND=%s)", num, hwnd)

    if _active_session_num == 0:
        _active_session_num = num
        if hwnd:
            _update_active_hwnd(hwnd)
        log.info("Session %d is now active", num)

    if _call.active:
        asyncio.create_task(_speak_tool(f"Terminal {num} connected."))

    return {"status": "ok", "num": num, "active": _active_session_num == num}


# ── /session/deregister — called by claude-voice.ps1 on exit ──────────────────

@app.post("/session/deregister")
async def session_deregister(request: Request) -> dict:
    global _active_session_num
    if not _session_authorized(request):
        log.warning("Rejected /session/deregister with a bad or missing token")
        return Response(status_code=403)
    body = await request.json()
    num = int(body.get("session_num", 0))
    if num not in _sessions:
        return {"status": "not-found"}

    del _sessions[num]
    log.info("Session %d deregistered", num)

    if _active_session_num == num:
        _active_session_num = min(_sessions.keys()) if _sessions else 0
        if _active_session_num:
            _update_active_hwnd(_sessions[_active_session_num]["hwnd"])
            if _call.active:
                asyncio.create_task(_speak_content(
                    f"Terminal {num} closed. Switched to terminal {_active_session_num}."
                ))
        else:
            if _call.active:
                asyncio.create_task(_speak_content(f"Terminal {num} closed. No active terminals."))

    return {"status": "ok"}

# ── /hook/tool-use — PreToolUse hook receiver ─────────────────────────────────

@app.post("/hook/tool-use")
async def tool_use(request: Request) -> dict:
    client_ip = request.client.host if request.client else ""
    if client_ip not in ("127.0.0.1", "::1"):
        return Response(status_code=403)

    if not _call.active:
        return {"status": "no-call"}

    body = await request.json()
    session_num = int(body.get("session_num", 0))

    # Ignore tool noise from background sessions entirely
    if session_num and _active_session_num and session_num != _active_session_num:
        return {"status": "background"}

    tool: str = body.get("tool", "")
    inp: dict = body.get("input", {})
    phrase = _build_tool_phrase(tool, inp)
    if phrase:
        asyncio.create_task(_speak_tool(phrase))
    return {"status": "ok"}

# ── /hook/assistant-text — Stop hook receiver (fallback) ──────────────────────

@app.post("/hook/assistant-text")
async def assistant_text(request: Request) -> dict:
    client_ip = request.client.host if request.client else ""
    if client_ip not in ("127.0.0.1", "::1"):
        return Response(status_code=403)

    body = await request.json()
    text: str = body.get("text", "").strip()
    tp: str = body.get("transcript_path", "")
    session_num: int = int(body.get("session_num", 0))

    global _transcript_path

    # Update session registry
    if session_num and session_num in _sessions:
        _sessions[session_num]["last_text"] = text
        _sessions[session_num]["last_time"] = asyncio.get_event_loop().time()
        if tp:
            _sessions[session_num]["transcript_path"] = tp

    # Update transcript path for active session
    if tp and (not session_num or not _active_session_num or session_num == _active_session_num):
        _transcript_path = tp

    # Stop watcher — turn is done
    if _watcher_task and not _watcher_task.done():
        _watcher_task.cancel()

    # Turn is done: back to silence.
    _stop_typing()

    if not text or not _call.active:
        return {"status": "no-call"}

    # Background session — short notification only
    is_background = (
        session_num
        and _active_session_num
        and session_num != _active_session_num
    )
    if is_background:
        first = text.split(".")[0][:100].strip()
        asyncio.create_task(_speak_tool(f"Terminal {session_num}: {first}."))
        return {"status": "background-notified"}

    # Active session — skip if watcher already spoke
    if _watcher_spoke_this_turn:
        log.info("Watcher already spoke this turn — Stop hook skipping")
        return {"status": "watcher-spoke"}

    asyncio.create_task(_speak_content(text))
    return {"status": "queued", "chars": len(text)}

# ── /health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health(request: Request):
    # Reports window handles, transcript paths and which session is live. That
    # is a map of the machine, so it needs the same token as /session/*.
    if not _session_authorized(request):
        return Response(status_code=403)
    return {
        "status": "ok",
        "call_active": _call.active,
        "active_session": _active_session_num,
        "sessions": {str(n): {"hwnd": s["hwnd"], "last_time": s["last_time"]} for n, s in _sessions.items()},
        "transcript_path": _transcript_path,
        "watcher_running": bool(_watcher_task and not _watcher_task.done()),
        "heartbeat_running": bool(_heartbeat_task and not _heartbeat_task.done()),
        "typing_running": bool(_typing_task and not _typing_task.done()),
        "turn_active": _turn_active,
    }
