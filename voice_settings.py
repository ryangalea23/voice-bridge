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

# Deepgram sometimes loses the first sound of an utterance, so "stop" arrives as
# "top" and the bridge typed it as a prompt instead of stopping. These are the
# extra spellings that count as the same command. This is a fixed list on
# purpose: a fuzzy or threshold match would make "top of the file" a coin flip,
# and a stop word has to be predictable. An alias only counts when its command
# is one of the stop words in use.
DEFAULT_STOP_ALIASES: dict[str, tuple[str, ...]] = {
    "stop": ("top", "op", "stopp", "stahp"),
    "cancel": ("ancel", "cansel"),
    "wait": ("ait", "weight"),
    "hold on": ("old on", "holdon"),
    "never mind": ("nevermind", "ever mind"),
}

# on: talking over the bridge also presses Escape, so the caller cutting in
# stops the work as well as the voice. off: it only stops the voice.
DEFAULT_BARGE_IN_STOPS_CLAUDE = True

# on: decide barge-in from Deepgram's interim transcripts, which land a few
# hundred milliseconds into a sentence. off: wait for the finished utterance,
# which needs UTTERANCE_END_MS of silence and so cannot arrive until the caller
# stops talking. On a real call that left the bridge talking over the caller for
# nearly four seconds.
DEFAULT_BARGE_IN_ON_INTERIM = True


@dataclass(frozen=True)
class VoiceSettings:
    ack_mode: str = DEFAULT_ACK
    readback: str = DEFAULT_READBACK
    haiku_timeout_ms: int = DEFAULT_HAIKU_TIMEOUT_MS
    stop_words: tuple[str, ...] = DEFAULT_STOP_WORDS
    stop_aliases: tuple[str, ...] = field(default_factory=tuple)
    barge_in_stops_claude: bool = DEFAULT_BARGE_IN_STOPS_CLAUDE
    barge_in_on_interim: bool = DEFAULT_BARGE_IN_ON_INTERIM
    deepgram_keyterms: tuple[str, ...] = field(default_factory=tuple)
    anthropic_api_key: str = ""


def _split_list(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


_TRUE = ("on", "true", "1", "yes")
_FALSE = ("off", "false", "0", "no")


def _load_flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    log.warning(
        "%s=%r is not on or off - using %s", name, raw, "on" if default else "off"
    )
    return default


def _parse_alias_map(raw: str) -> dict[str, tuple[str, ...]]:
    """Parse VOICE_STOP_ALIASES, e.g. "stop=top|op,wait=ait"."""
    out: dict[str, tuple[str, ...]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        command, _, spellings = entry.partition("=")
        command = " ".join(command.lower().split())
        parsed = tuple(
            " ".join(s.lower().split()) for s in spellings.split("|") if s.strip()
        )
        if command and parsed:
            out[command] = out.get(command, ()) + parsed
    return out


def _load_stop_aliases(
    env: Mapping[str, str], stop_words: tuple[str, ...]
) -> tuple[str, ...]:
    """The extra spellings that count as a stop word, for the words in use only."""
    raw = env.get("VOICE_STOP_ALIASES")
    if raw is None:
        alias_map = DEFAULT_STOP_ALIASES
    elif not raw.strip():
        # An explicit empty value turns the aliases off.
        return ()
    else:
        alias_map = _parse_alias_map(raw)
        if not alias_map:
            log.warning(
                "VOICE_STOP_ALIASES=%r has no command=spelling pairs - using the defaults",
                raw,
            )
            alias_map = DEFAULT_STOP_ALIASES

    words = set(stop_words)
    out: list[str] = []
    for command, spellings in alias_map.items():
        if command not in words:
            continue
        for spelling in spellings:
            if spelling not in words and spelling not in out:
                out.append(spelling)
    return tuple(out)


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
        stop_aliases=_load_stop_aliases(env, stop_words),
        barge_in_stops_claude=_load_flag(
            env, "BARGE_IN_STOPS_CLAUDE", DEFAULT_BARGE_IN_STOPS_CLAUDE
        ),
        barge_in_on_interim=_load_flag(
            env, "BARGE_IN_ON_INTERIM", DEFAULT_BARGE_IN_ON_INTERIM
        ),
        deepgram_keyterms=_split_list(env.get("DEEPGRAM_KEYTERMS", "")),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
    )


def log_settings(s: VoiceSettings) -> None:
    log.info(
        "Voice settings: ACK_MODE=%s READBACK=%s READBACK_HAIKU_TIMEOUT_MS=%d "
        "VOICE_STOP_WORDS=%s VOICE_STOP_ALIASES=%s BARGE_IN_STOPS_CLAUDE=%s "
        "BARGE_IN_ON_INTERIM=%s DEEPGRAM_KEYTERMS=%s ANTHROPIC_API_KEY=%s",
        s.ack_mode,
        s.readback,
        s.haiku_timeout_ms,
        ",".join(s.stop_words),
        ",".join(s.stop_aliases) or "<none>",
        "on" if s.barge_in_stops_claude else "off",
        "on" if s.barge_in_on_interim else "off",
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
