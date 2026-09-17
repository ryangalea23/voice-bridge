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
DEFAULT_HAIKU_TIMEOUT_MS = 2500
DEFAULT_STOP_WORDS = ("stop", "cancel", "wait", "hold on", "never mind")


@dataclass(frozen=True)
class VoiceSettings:
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
        readback=readback,
        haiku_timeout_ms=timeout_ms,
        stop_words=stop_words,
        deepgram_keyterms=_split_list(env.get("DEEPGRAM_KEYTERMS", "")),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
    )


def log_settings(s: VoiceSettings) -> None:
    log.info(
        "Voice settings: READBACK=%s READBACK_HAIKU_TIMEOUT_MS=%d VOICE_STOP_WORDS=%s "
        "DEEPGRAM_KEYTERMS=%s ANTHROPIC_API_KEY=%s",
        s.readback,
        s.haiku_timeout_ms,
        ",".join(s.stop_words),
        ",".join(s.deepgram_keyterms) or "<none>",
        "set" if s.anthropic_api_key else "<not set>",
    )
    if s.readback == "haiku" and not s.anthropic_api_key:
        log.warning(
            "READBACK=haiku but ANTHROPIC_API_KEY is not set - every turn will "
            "fall back to the transcript read-back"
        )
