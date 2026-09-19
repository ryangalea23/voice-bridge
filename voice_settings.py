"""Settings for the mishearing and safety features, read once at startup.

Every setting has a safe default. A value that does not parse falls back to
that default and logs a warning naming the bad value, so a typo in .env never
silently turns a safety feature off or crashes the bridge.

CONFIRM_RISKY and VOICE_COORDINATOR are not here: they shape the Claude Code
system prompt, so claude-voice.ps1 reads them when it launches the session.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Mapping

log = logging.getLogger(__name__)

READBACK_MODES = ("off", "transcript", "haiku")
DEFAULT_READBACK = "transcript"
# What the bridge says the moment your words are typed in.
#   off       nothing, just the typing sound. The default, because a canned
#             "Yup." right after a question sounds like an answer to it.
#   short     one of the canned phrases.
#   readback  the read-back, in whichever flavour READBACK names.
# ACK_MODE decides WHETHER anything is said; READBACK only picks the flavour of
# the read-back, and only when ACK_MODE=readback. So ACK_MODE wins.
ACK_MODES = ("off", "short", "readback")
DEFAULT_ACK = "off"
DEFAULT_HAIKU_TIMEOUT_MS = 2500
DEFAULT_STOP_WORDS = ("stop", "cancel", "wait", "hold on", "never mind")


@dataclass(frozen=True)
class VoiceSettings:
    ack_mode: str = DEFAULT_ACK
    readback: str = DEFAULT_READBACK
    haiku_timeout_ms: int = DEFAULT_HAIKU_TIMEOUT_MS
    stop_words: tuple[str, ...] = DEFAULT_STOP_WORDS
    deepgram_keyterms: tuple[str, ...] = field(default_factory=tuple)
    anthropic_api_key: str = ""


def _split_list(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def load_settings(env: Mapping[str, str] | None = None) -> VoiceSettings:
    env = os.environ if env is None else env

    readback = env.get("READBACK", DEFAULT_READBACK).strip().lower() or DEFAULT_READBACK
    if readback not in READBACK_MODES:
        log.warning(
            "READBACK=%r is not one of %s - using %r",
            readback, "|".join(READBACK_MODES), DEFAULT_READBACK,
        )
        readback = DEFAULT_READBACK

    ack_mode = env.get("ACK_MODE", DEFAULT_ACK).strip().lower() or DEFAULT_ACK
    if ack_mode not in ACK_MODES:
        log.warning(
            "ACK_MODE=%r is not one of %s - using %r",
            ack_mode, "|".join(ACK_MODES), DEFAULT_ACK,
        )
        ack_mode = DEFAULT_ACK

    raw_timeout = env.get("READBACK_HAIKU_TIMEOUT_MS", "").strip()
    timeout_ms = DEFAULT_HAIKU_TIMEOUT_MS
    if raw_timeout:
        try:
            timeout_ms = int(raw_timeout)
            if timeout_ms <= 0:
                raise ValueError
        except ValueError:
            log.warning(
                "READBACK_HAIKU_TIMEOUT_MS=%r is not a positive whole number - using %d",
                raw_timeout, DEFAULT_HAIKU_TIMEOUT_MS,
            )
            timeout_ms = DEFAULT_HAIKU_TIMEOUT_MS

    stop_words = DEFAULT_STOP_WORDS
    raw_stop = env.get("VOICE_STOP_WORDS")
    if raw_stop is not None and raw_stop.strip():
        parsed = tuple(" ".join(w.lower().split()) for w in _split_list(raw_stop))
        if parsed:
            stop_words = parsed
        else:
            log.warning("VOICE_STOP_WORDS=%r has no words - using the defaults", raw_stop)

    return VoiceSettings(
        ack_mode=ack_mode,
        readback=readback,
        haiku_timeout_ms=timeout_ms,
        stop_words=stop_words,
        deepgram_keyterms=_split_list(env.get("DEEPGRAM_KEYTERMS", "")),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
    )


def log_settings(s: VoiceSettings) -> None:
    log.info(
        "Voice settings: ACK_MODE=%s READBACK=%s READBACK_HAIKU_TIMEOUT_MS=%d "
        "VOICE_STOP_WORDS=%s DEEPGRAM_KEYTERMS=%s ANTHROPIC_API_KEY=%s",
        s.ack_mode,
        s.readback,
        s.haiku_timeout_ms,
        ",".join(s.stop_words),
        ",".join(s.deepgram_keyterms) or "<none>",
        "set" if s.anthropic_api_key else "<not set>",
    )
    if s.ack_mode == "readback" and s.readback == "off":
        log.warning(
            "ACK_MODE=readback but READBACK=off, so there is no read-back to say - "
            "the bridge will stay quiet until the answer is ready, same as ACK_MODE=off"
        )
    if s.ack_mode == "readback" and s.readback == "haiku" and not s.anthropic_api_key:
        log.warning(
            "READBACK=haiku but ANTHROPIC_API_KEY is not set - every turn will "
            "fall back to the transcript read-back"
        )
