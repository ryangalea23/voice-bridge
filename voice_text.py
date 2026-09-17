"""Shared text cleanup for voice input, used by both the phone bridge and the
desk mic.

Filler-word stripping started out duplicated — once in bridge.py, once
copy-pasted into desk_mic.py with a "keep these in sync" comment. A comment
does not enforce anything; this module does, by being the one place the
regex lives. Both bridge.py and desk_mic.py import from here.
"""
import re

FILLER_RE = re.compile(r"\b(um+|uh+|hmm+)\b[,.]?\s*", re.IGNORECASE)


def strip_fillers(text: str) -> str:
    return FILLER_RE.sub(" ", text).strip()


def is_stop_command(text: str, stop_words) -> bool:
    """True only when the WHOLE utterance is a stop word, e.g. "Stop." or
    "hold on". "stop the server" is a real request and must not match."""
    norm = " ".join(text.lower().split()).rstrip(".!?,;: ")
    return norm in stop_words
