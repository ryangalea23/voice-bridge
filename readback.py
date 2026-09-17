"""Read-back: tell the caller what the bridge heard, so a mishearing can be
caught and stopped before it does damage.

Two flavours:
  transcript  "I heard: <the cleaned transcript>", cut to about 20 words.
  haiku       a short restatement from Claude Haiku. It only restates the
              request, it never answers it. Any failure returns None and the
              caller falls back to the transcript read-back.

The Haiku call uses urllib so the bridge needs no extra dependency.
"""
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
MAX_READBACK_WORDS = 20
STOP_HINT = "Say stop to cancel."

HAIKU_SYSTEM = (
    "You restate a spoken request so the speaker can confirm it was heard "
    "correctly. Reply with only the restatement, in at most 15 plain words, "
    "starting with a verb or noun, no preamble. Never answer the request, "
    "never add facts, advice or details that are not in it. If it is garbled, "
    "restate the words as heard."
)


class HaikuError(Exception):
    """Raised when the Haiku read-back cannot be used. The message says why."""


def truncate_words(text: str, max_words: int = MAX_READBACK_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + " and more"


def transcript_readback(text: str) -> str:
    return f"I heard: {truncate_words(text)}"


def haiku_restate(text: str, api_key: str, timeout_s: float) -> str:
    """Blocking. Run it in an executor. Raises HaikuError on any failure."""
    if not api_key:
        raise HaikuError("ANTHROPIC_API_KEY is not set")
    body = json.dumps({
        "model": HAIKU_MODEL,
        "max_tokens": 60,
        "system": HAIKU_SYSTEM,
        "messages": [{"role": "user", "content": text}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            # api.anthropic.com can refuse urllib's default User-Agent.
            "user-agent": "voice-bridge/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise HaikuError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HaikuError(f"request failed: {exc}") from exc
    except ValueError as exc:
        raise HaikuError(f"bad JSON: {exc}") from exc

    parts = [
        b.get("text", "")
        for b in payload.get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    out = " ".join("".join(parts).split())
    if not out:
        raise HaikuError("empty output")
    return out
