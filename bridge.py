"""Twilio voice bridge — routes phone calls to Claude Code CLI and back.

Routes:
  POST /twilio/voice          — Twilio webhook; returns TwiML to open Media Stream.
                                Answers inbound calls, and outbound calls placed by
                                outbound-call.ps1. On an outbound call it reads Twilio's
                                AnsweredBy and hangs up on a machine, so a voicemail
                                greeting is never transcribed.
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

from inject import inject_prompt, send_escape, session_error
import readback
from stt import DeepgramSTT
from tts import text_to_mulaw_chunks
from voice_settings import load_settings, log_settings
from voice_text import (
    MIN_ECHO_CHARS,
    MIN_ECHO_WORDS,
    is_echo_of,
    is_stop_command,
    strip_fillers,
)
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

# Only used when ACK_MODE=short. On a real call a canned "Yup." right after a
# question sounded like the answer to it, so the default is to say nothing.
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

# ── Interrupted turns ──────────────────────────────────────────────────────────
#
# Cutting the bridge off used to stop only the voice. The turn kept running, and
# when it finished the answer arrived at /hook/assistant-text and was spoken from
# the top - the caller had moved on a sentence ago. So every interruption that
# lands while a turn is in flight writes one entry here, and the next reply to
# reach us is that turn's reply and gets dropped.
#
# The queue is what makes "cut in, then immediately say the next thing" work.
# Claude answers in the order it was asked, so the first reply after an
# interruption belongs to the cut-off turn and the one after it belongs to the
# new request. One entry, one dropped reply, and the new request is spoken.
#
# The Escape case needs a time limit. A turn that was stopped with Escape often
# leaves a part-written answer behind, which arrives within a second or two; if
# nothing arrives in that window the turn died silently and the entry has to go,
# or it would swallow a later, perfectly good answer.
_ESCAPED_DROP_GRACE_S = 4.0

# Each entry: {"at": float, "escaped": bool}
_pending_drops: list[dict] = []
# One decision per reply, reused for every block of that same reply. A long
# answer reaches the watcher in pieces and they all belong to one turn.
_reply_verdict: Optional[bool] = None
_turn_seq: int = 0
_reply_verdict_turn: int = -1

# ── Echo guard ─────────────────────────────────────────────────────────────────
#
# The caller's phone speaker plays our own voice back into its microphone, so
# Deepgram transcribes what the bridge just said and the bridge treats it as a
# new request. On a real call that made an endless read-back loop.
#
# The first version of this guard dropped EVERY utterance that arrived while our
# audio was playing. It killed the loop and it killed barge-in with it: the
# caller could no longer cut the bridge off mid-answer, which is the feature
# people use most. So the guard no longer asks "is our audio playing", it asks
# "are these our own words".
#
#   1. A playback clock says when our audio is on the line. Outbound mu-law is
#      8000 bytes per second, so every chunk we queue says exactly how long it
#      will play. That window (plus an ECHO_GUARD_MS tail for audio already in
#      Twilio's buffer) is when echo is possible. It is arithmetic on what we
#      sent, not a guess.
#   2. Inside that window every transcript is compared with what we are saying
#      and what we said just before. A match is echo and is dropped. Anything
#      else is the caller talking over us: clear the audio, cancel the sentence,
#      and handle the words normally. This runs on PARTIAL transcripts too
#      (_on_interim), because a finished utterance needs UTTERANCE_END_MS of
#      silence and so cannot arrive while the caller is still speaking.
#   3. Outside the window the same text test still runs, for echo that lands
#      after the tail.
#
# Stop words are checked before all of it, because interrupting is the point.
# The text test has a minimum length (voice_text.MIN_ECHO_CHARS / MIN_ECHO_WORDS)
# so a short real answer - "yes", "no", "sure" - is never mistaken for echo.

MULAW_BYTES_PER_SECOND = 8000
_DEFAULT_ECHO_GUARD_MS = 1200


def _read_echo_guard_ms() -> int:
    raw = os.environ.get("ECHO_GUARD_MS", "").strip()
    if not raw:
        return _DEFAULT_ECHO_GUARD_MS
    try:
        value = int(raw)
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        log.warning(
            "ECHO_GUARD_MS=%r is not a whole number of milliseconds - using %d",
            raw, _DEFAULT_ECHO_GUARD_MS,
        )
        return _DEFAULT_ECHO_GUARD_MS


ECHO_GUARD_MS = _read_echo_guard_ms()
log.info("Echo guard: %dms tail after outbound audio stops", ECHO_GUARD_MS)

_playback_until: float = 0.0   # loop time when the audio we have queued runs out
_last_spoken_text: str = ""    # exactly what the bridge is saying, or said last
_last_spoken_at: float = 0.0   # when, so the echo net expires instead of lasting forever
_prev_spoken_text: str = ""    # the one before that; echo often lags a sentence


def _now() -> float:
    return asyncio.get_event_loop().time()


def _note_outbound_audio(chunk: bytes) -> None:
    """Advance the playback clock by this chunk's real duration."""
    global _playback_until
    _playback_until = max(_playback_until, _now()) + len(chunk) / MULAW_BYTES_PER_SECOND


def _reset_playback_clock() -> None:
    """Queued audio was thrown away, so it will never play."""
    global _playback_until
    _playback_until = 0.0


def _remember_spoken(text: str) -> None:
    global _last_spoken_text, _prev_spoken_text, _last_spoken_at
    _last_spoken_at = _now()
    if text != _last_spoken_text:
        _prev_spoken_text = _last_spoken_text
    _last_spoken_text = text


def _forget_spoken() -> None:
    """New call, so nothing we said before can be echoing now."""
    global _last_spoken_text, _prev_spoken_text
    _last_spoken_text = ""
    _prev_spoken_text = ""


# Interims keep arriving as a sentence grows: "no", "no i it's", "no i told you
# not to". Only the first one that clears the tests may barge in, or the caller
# would get an Escape per word. The latch lifts when the finished utterance
# lands, so the next sentence can interrupt again.
_interim_barged: bool = False


def _reset_interim_latch() -> None:
    global _interim_barged
    _interim_barged = False


def _audio_playing() -> bool:
    """True while the audio we queued is still on the line."""
    return _now() < _playback_until


def _echo_guard_active() -> bool:
    """True while our own audio plays, plus the tail after it stops. Inside this
    window echo is possible, so the text test below decides. Outside it, echo is
    only possible from audio we have long forgotten."""
    return _now() < _playback_until + ECHO_GUARD_MS / 1000.0


# How long after speaking our own words can still come back to us. Echo showed
# up about 5 seconds late on a real call, so 10 seconds is comfortable, while
# still letting the caller repeat a phrase we used once the moment has passed.
ECHO_MEMORY_SECONDS = float(os.environ.get("ECHO_MEMORY_SECONDS", "10"))


def _looks_like_echo(text: str) -> bool:
    """True when the utterance is our own voice coming back, matched against what
    we are saying now and the sentence before it."""
    # The net remembers what we said for a short while, not forever. Echo can
    # arrive a few seconds late, but a minute later the caller is simply
    # talking. Without this limit, once Claude quoted the caller back ("I only
    # caught 'why weren't you'"), the caller repeating that phrase was binned as
    # echo and the call went dead.
    if _now() > _last_spoken_at + ECHO_MEMORY_SECONDS:
        return False
    if is_echo_of(text, _last_spoken_text):
        return True
    # The earlier sentence only counts while echo is still possible - the mic can
    # run a sentence behind us. Later on it is just something we said a while ago,
    # and the caller is free to say it back on purpose.
    return _echo_guard_active() and is_echo_of(text, _prev_spoken_text)


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
        _reset_playback_clock()
        _forget_spoken()
        _forget_interrupted_turns()
        _reset_interim_latch()

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


def _hangup_twiml() -> str:
    """No greeting, no stream, nothing typed anywhere. Just go away."""
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


# Twilio's answering-machine detection reports what picked up in AnsweredBy.
# Anything but a person means there is no one to talk to, so the bridge hangs up
# rather than transcribing a voicemail greeting and typing it into the terminal.
# "unknown" and an empty value mean detection did not run or could not decide,
# and those are treated as a person so a real call is never dropped.
_HUMAN_ANSWERS = ("human", "unknown", "")


def _is_outbound(params: dict) -> bool:
    """Twilio sends Direction=inbound for a call to the number, and
    outbound-api / outbound-dial when something placed the call."""
    return params.get("Direction", "inbound").lower().startswith("outbound")


def _machine_answered(params: dict) -> str:
    """The AnsweredBy value when a machine picked up, else an empty string.

    Only ever consulted for outbound calls. An inbound call never carries
    AnsweredBy, and is never asked about it, so this cannot change what happens
    when the phone rings here.
    """
    if not _is_outbound(params):
        return ""
    answered_by = params.get("AnsweredBy", "").strip().lower()
    return "" if answered_by in _HUMAN_ANSWERS else answered_by


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
    # nothing about who is on the phone, so check the number before handing it a
    # live terminal. On an inbound call that is From, the person dialling. On an
    # outbound call From is our own Twilio number and the person is To, so the
    # allowlist is checked against the number that was dialled. Either way the
    # human end of the call has to be on the list, and an empty list still
    # rejects everything.
    outbound = _is_outbound(params)
    human_end = params.get("To", "") if outbound else params.get("From", "")
    if not _caller_allowed(human_end):
        log.warning(
            "Rejected %s call with %s (not in ALLOWED_CALLERS)",
            "outbound" if outbound else "inbound",
            human_end or "<unknown>",
        )
        return Response(content=_reject_twiml(), media_type="application/xml")

    # Outbound only: if a machine picked up, hang up. Nothing is transcribed and
    # nothing is typed, which is the whole point - a voicemail greeting used to
    # be injected as a prompt.
    machine = _machine_answered(params)
    if machine:
        log.warning(
            "Outbound call to %s was answered by %s - hanging up, nothing injected",
            human_end, machine,
        )
        return Response(content=_hangup_twiml(), media_type="application/xml")

    log.info(
        "Accepted %s call with %s%s",
        "outbound" if outbound else "inbound",
        human_end,
        f" (answered by {params.get('AnsweredBy', '')})" if outbound and params.get("AnsweredBy") else "",
    )

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

def _drain_outbound() -> None:
    """Throw away audio we queued but have not sent, and stop the playback clock
    counting it. Sent-but-not-yet-played audio needs the Twilio clear event on
    top; see _clear_audio."""
    if _call.outbound_q is not None:
        while not _call.outbound_q.empty():
            try:
                _call.outbound_q.get_nowait()
            except asyncio.QueueEmpty:
                break
    _reset_playback_clock()


async def _speak_tool(text: str) -> None:
    """Low-priority: progress phrases and heartbeat. Skipped if content is speaking."""
    global _tool_version, _last_speech_time
    if not _call.active or _call.speaking:
        return
    global _tool_busy
    _tool_version += 1
    my_version = _tool_version
    _last_speech_time = asyncio.get_event_loop().time()
    _remember_spoken(text)
    _tool_busy += 1
    try:
        async for chunk in text_to_mulaw_chunks(text):
            if _tool_version != my_version or _call.interrupted or not _call.active:
                return
            await _call.outbound_q.put(chunk)
            _note_outbound_audio(chunk)
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
    # Drop our queue AND tell Twilio to drop what we already sent it. The typing
    # sound is pushed ahead of real time, so Twilio can be holding several
    # seconds of it. Without the clear event the caller sits through all of that
    # before hearing the answer, which sounded like the bridge was slow when the
    # reply had actually been ready for ten seconds.
    await _clear_audio()

    if not _call.active:
        return

    log.info("Speaking %d chars: %r", len(text), text[:70])
    _chunks_sent = 0
    _last_speech_time = asyncio.get_event_loop().time()
    _call.speaking = True
    _call.interrupted = False
    _call.speak_since = _last_speech_time
    _remember_spoken(text)
    try:
        async for chunk in text_to_mulaw_chunks(text):
            if _content_version != my_version or _call.interrupted or not _call.active:
                log.info("Content speak cancelled (v%d, mine=%d)", _content_version, my_version)
                return
            await _call.outbound_q.put(chunk)
            _note_outbound_audio(chunk)
            _chunks_sent += 1
    except Exception as exc:
        log.error("Content speak error: %s", exc)
    finally:
        log.info("Speech finished: %d chunks queued (%.1fs of audio)",
                 _chunks_sent, _chunks_sent * 0.2)
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
    global _typing_task, _turn_active, _turn_seq
    _turn_active = True
    # A new turn, so whatever was decided about the last turn's reply does not
    # apply to this one.
    _turn_seq += 1
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
            # Binary, not text. os.path.getsize counts BYTES while a text read
            # returns CHARACTERS, so one emoji or curly quote in the transcript
            # put the next seek before where we had already read, and the
            # watcher spoke old replies again.
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
            pos += len(chunk)
            new_data = chunk.decode("utf-8", errors="replace")
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

            if not _should_speak_reply(cleaned):
                continue

            log.info("Watcher speaking %d chars from byte %d: %r",
                     len(cleaned), pos, cleaned[:70])
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
    _drain_outbound()
    if _call.ws and _call.stream_sid:
        try:
            await _call.ws.send_text(json.dumps({
                "event": "clear",
                "streamSid": _call.stream_sid,
            }))
            log.info("Sent Twilio clear — audio stopped")
        except Exception as exc:
            log.warning("Could not send clear: %s", exc)


def _stop_spellings() -> tuple[str, ...]:
    """Every spelling that counts as a stop word: the words themselves plus the
    clipped forms Deepgram hands back, such as "top" for "stop"."""
    return tuple(SETTINGS.stop_words) + tuple(SETTINGS.stop_aliases)


def _forget_interrupted_turns() -> None:
    global _reply_verdict, _reply_verdict_turn
    _pending_drops.clear()
    _reply_verdict = None
    _reply_verdict_turn = -1


def _note_interrupted_turn(escaped: bool) -> None:
    """Remember that the turn in flight was cut off, so when its answer finally
    arrives the bridge stays quiet instead of reading out old news. Nothing to
    remember when no turn is running: there is no reply on its way."""
    if not _turn_active:
        return
    _pending_drops.append({"at": _now(), "escaped": escaped})
    log.info(
        "Turn interrupted%s - its reply will be dropped",
        " with Escape" if escaped else "",
    )


def _forget_stale_drops() -> None:
    """Give up on a stopped turn that never sent anything back."""
    while _pending_drops:
        head = _pending_drops[0]
        if not head["escaped"]:
            # Not stopped, only talked over, so it is still working and its reply
            # is still coming. Wait for it however long it takes.
            return
        if _now() - head["at"] <= _ESCAPED_DROP_GRACE_S:
            return
        _pending_drops.pop(0)
        log.info("A stopped turn sent nothing back - the next reply is fair game")


def _should_speak_reply(text: str) -> bool:
    """False when this reply belongs to a turn the caller cut off."""
    global _reply_verdict, _reply_verdict_turn
    if _reply_verdict is not None and _reply_verdict_turn == _turn_seq:
        return _reply_verdict
    _forget_stale_drops()
    verdict = True
    if _pending_drops:
        _pending_drops.pop(0)
        verdict = False
        first_words = " ".join(text.split())[:60]
        log.info("Dropped the reply to an interrupted turn: %r", first_words)
    _reply_verdict = verdict
    _reply_verdict_turn = _turn_seq
    return verdict


def _end_reply_stream() -> None:
    """The turn is over, so the next reply gets its own decision."""
    global _reply_verdict, _reply_verdict_turn
    _reply_verdict = None
    _reply_verdict_turn = -1


async def _barge_in(reason: str) -> None:
    """The caller talked over us and it is not our own voice coming back. Stop
    speaking at once: cancel the sentence in flight, drop our queue, and tell
    Twilio to throw away what it is already holding.

    With BARGE_IN_STOPS_CLAUDE=on this also presses Escape, so cutting in stops
    the work and not just the voice. The Escape goes out on its own task: it
    reaches into a console window and can take a moment, and nothing may delay
    the audio going quiet."""
    global _content_version, _tool_version
    log.info("Barge-in: %s", reason)
    # Bumping both versions makes the running _speak_content and _speak_tool
    # loops see they are stale and stop feeding chunks.
    _content_version += 1
    _tool_version += 1
    _call.interrupted = True
    # The cancelled _speak_content will not clear this itself - its own version
    # check no longer matches - so clear it here.
    _call.speaking = False
    stops_claude = SETTINGS.barge_in_stops_claude and _turn_active
    _note_interrupted_turn(escaped=stops_claude)
    if stops_claude:
        # Stop the typing sound here rather than on the Escape task. The caller
        # often says the next thing straight away, and a _stop_typing arriving
        # late would silence the new turn instead of the old one.
        _stop_typing()
    await _clear_audio()
    if stops_claude:
        asyncio.create_task(_escape_after_barge_in())


async def _escape_after_barge_in() -> None:
    """Press Escape for a barge-in, after the audio has already stopped."""
    ok = await asyncio.get_event_loop().run_in_executor(None, send_escape)
    if ok:
        log.info("Barge-in sent Escape - the turn is stopped")
    else:
        log.warning("Barge-in could not send Escape - the turn keeps running")


async def _on_speech_started() -> None:
    if not _call.active:
        return
    if _audio_playing():
        # Speech started is just voice energy; there is no transcript yet, so our
        # own voice and the caller's are indistinguishable here. Clearing now
        # would chop our sentence off every time the phone speaker fed us back.
        # _on_interim runs the text test on the first partial transcript, a few
        # hundred milliseconds later, and barges in there.
        log.info("Speech started while our audio plays - deciding at the transcript")
        return
    elapsed = asyncio.get_event_loop().time() - _call.speak_since
    if elapsed > 3.0:
        log.info("User speaking at elapsed=%.1fs — clearing buffer", elapsed)
        _call.interrupted = True
        await _clear_audio()

def _interim_is_substantial(text: str) -> bool:
    """True when a partial transcript is long enough to judge.

    The floor is the same one the echo test uses (voice_text.MIN_ECHO_CHARS /
    MIN_ECHO_WORDS), and on purpose. Under it, is_echo_of always answers "not
    echo", so a one-word scrap of our own sentence coming back would look
    exactly like the caller cutting in. Below the floor the bridge cannot tell
    the two apart, so it waits for the next interim - which is a few hundred
    milliseconds away, not four seconds.
    """
    return len(text) >= MIN_ECHO_CHARS and len(text.split()) >= MIN_ECHO_WORDS


async def _on_interim(text: str) -> None:
    """A partial transcript, while the caller is still talking.

    This is what makes interrupting feel instant. The finished utterance cannot
    arrive until UTTERANCE_END_MS of silence, so on a real call the bridge kept
    talking for nearly four seconds while the caller said "no I told you not to
    stop, you're still talking". Interims land a few hundred milliseconds in,
    and they carry words, so the same text test that separates our own voice
    from the caller's can run on them.

    The interim text is never injected. The finished utterance still goes
    through _on_utterance exactly as before.
    """
    global _interim_barged
    if not SETTINGS.barge_in_on_interim:
        return
    if not _call.active or _interim_barged:
        return
    # Only while our own audio is on the line. Nothing to interrupt otherwise,
    # and _on_utterance handles everything else a moment later.
    if not _audio_playing():
        return
    text = strip_fillers(text)
    if not text or not _interim_is_substantial(text):
        return
    if _looks_like_echo(text):
        log.info("Interim %r is our own voice coming back - not a barge-in", text)
        return
    # A stop word gets Escape from _handle_stop when the utterance finishes.
    # Barging in here as well would press Escape twice and count the turn as
    # interrupted twice, so one reply too many would be dropped.
    if is_stop_command(text, _stop_spellings()):
        log.info("Interim %r is a stop word - leaving it to the utterance path", text)
        return
    _interim_barged = True
    await _barge_in(f"caller spoke over us, from a partial transcript: {text!r}")


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


def _no_session_phrase(fallback_tail: str) -> str:
    """What to say when nothing can be typed. The launcher leaves a reason when
    it could not find a window that accepts typed text; that reason is more
    useful than "session not active"."""
    reason = session_error()
    if reason:
        return reason
    return f"Session not active. {fallback_tail}"


async def _handle_stop() -> None:
    """Interrupt the running turn instead of typing the stop word as a prompt."""
    log.info("Stop command - sending Escape")
    _note_interrupted_turn(escaped=True)
    ok = await asyncio.get_event_loop().run_in_executor(None, send_escape)
    _stop_typing()
    if ok:
        await _speak_content("Stopped.")
    else:
        await _speak_content(_no_session_phrase("Nothing to stop."))


async def _on_utterance(text: str) -> None:
    global _ack_idx, _watcher_spoke_this_turn
    # The sentence is over, so the next one is allowed its own interim barge-in.
    _reset_interim_latch()
    text = strip_fillers(text)
    if not text:
        return
    log.info("Utterance: %r", text)

    # Stop words are checked before the echo guard: interrupting has to work
    # while the bridge is talking, which is exactly when the guard is on.
    if is_stop_command(text, _stop_spellings()):
        await _handle_stop()
        return

    # Our own voice is only on the line while our audio plays, plus the tail. In
    # that window, and after it, the same question decides: are these our words?
    if _looks_like_echo(text):
        log.info(
            "Echo dropped %r - it repeats what the bridge just said (%r)",
            text, _last_spoken_text,
        )
        return

    # Not our words, and we are still talking, so the caller is cutting in.
    if _echo_guard_active():
        await _barge_in(f"caller spoke over us: {text!r}")

    if await _handle_voice_command(text):
        return

    # Start Haiku before injecting so the two run side by side. Injection never
    # waits on it.
    haiku_task = None
    if SETTINGS.ack_mode == "readback" and SETTINGS.readback == "haiku":
        haiku_task = asyncio.create_task(_haiku_restatement(text))

    ok = await asyncio.get_event_loop().run_in_executor(None, inject_prompt, text)
    if not ok:
        if haiku_task:
            haiku_task.cancel()
        await _speak_content(_no_session_phrase(
            "Please open Claude Code with the voice launcher."
        ))
    else:
        _watcher_spoke_this_turn = False
        # What to say the moment the text is in. ACK_MODE decides; the typing
        # sound runs underneath either way, so silence still sounds like work.
        if SETTINGS.ack_mode == "short":
            ack = _ACK_PHRASES[_ack_idx % len(_ACK_PHRASES)]
            _ack_idx += 1
            asyncio.create_task(_speak_content(ack))
        elif SETTINGS.ack_mode == "readback" and haiku_task:
            asyncio.create_task(_speak_haiku_readback(text, haiku_task))
        elif SETTINGS.ack_mode == "readback" and SETTINGS.readback != "off":
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
        on_interim=_on_interim,
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
        _end_reply_stream()
        return {"status": "watcher-spoke"}

    speak = _should_speak_reply(text)
    # The turn is over either way, so the next reply gets a fresh decision.
    _end_reply_stream()
    if not speak:
        return {"status": "interrupted"}

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
