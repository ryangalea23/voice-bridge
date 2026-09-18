"""Shared text cleanup for voice input, used by both the phone bridge and the
desk mic.

Filler-word stripping started out duplicated — once in bridge.py, once
copy-pasted into desk_mic.py with a "keep these in sync" comment. A comment
does not enforce anything; this module does, by being the one place the
regex lives. Both bridge.py and desk_mic.py import from here.
"""
import re

FILLER_RE = re.compile(r"\b(um+|uh+|hmm+)\b[,.]?\s*", re.IGNORECASE)

# Below this length, or as a single word, an utterance is too common to blame on
# the speaker echo: "yes" is an answer even right after the bridge said "Yes...".
# The loop that started this was "i heard" (two words, seven characters), so the
# bar has to sit below that.
MIN_ECHO_CHARS = 6
MIN_ECHO_WORDS = 2


def strip_fillers(text: str) -> str:
    return FILLER_RE.sub(" ", text).strip()


def normalize_for_echo(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Used only to compare an
    incoming utterance with what the bridge just said."""
    stripped = "".join(c if (c.isalnum() or c.isspace()) else " " for c in text.lower())
    return " ".join(stripped.split())


def is_echo_of(text: str, spoken: str, min_chars: int = MIN_ECHO_CHARS) -> bool:
    """True when `text` looks like the phone speaker playing `spoken` back.

    Equal, or a prefix of what was said, after normalising. Very short or
    single-word utterances are never treated as echo: "yes" is a real answer
    even when the bridge just said "Yes, the tests pass."
    """
    if not spoken:
        return False
    heard = normalize_for_echo(text)
    said = normalize_for_echo(spoken)
    if not said or len(heard) < min_chars or len(heard.split()) < MIN_ECHO_WORDS:
        return False
    return heard == said or said.startswith(heard + " ") or said.startswith(heard)


def is_stop_command(text: str, stop_words) -> bool:
    """True only when the WHOLE utterance is a stop word, e.g. "Stop." or
    "hold on". "stop the server" is a real request and must not match."""
    norm = " ".join(text.lower().split()).rstrip(".!?,;: ")
    return norm in stop_words
