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

    Matches any whole run of words inside what was said, after normalising: the
    whole thing, the start, the end, or a stretch out of the middle. Echo rarely
    catches a clean sentence - the mic picks it up part-way through and loses the
    end - so a prefix-only test let plenty of echo through.

    The floor below matters more than the match now. This test is also what
    decides whether the caller is interrupting, so treating a real word as echo
    silently swallows a request. Anything under MIN_ECHO_CHARS characters or
    MIN_ECHO_WORDS words is never echo: "yes", "no", "stop" and "wait" are
    answers even when the bridge just said "Yes, the tests pass."
    """
    if not spoken:
        return False
    heard = normalize_for_echo(text)
    said = normalize_for_echo(spoken)
    if not said or len(heard) < min_chars or len(heard.split()) < MIN_ECHO_WORDS:
        return False
    # Pad both sides so the run has to line up on word boundaries: "run the"
    # must not match inside "overrun theatre".
    return f" {heard} " in f" {said} "


def is_stop_command(text: str, stop_words) -> bool:
    """True only when the WHOLE utterance is a stop word, e.g. "Stop." or
    "hold on". "stop the server" is a real request and must not match."""
    norm = " ".join(text.lower().split()).rstrip(".!?,;: ")
    return norm in stop_words
