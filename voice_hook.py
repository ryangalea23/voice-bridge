"""Claude Code Stop hook: sends the latest assistant reply to the voice bridge.

Claude Code runs this after every assistant turn and feeds it the hook JSON on
stdin, which includes `transcript_path`. This script reads the last assistant
message out of that transcript and POSTs it to the bridge's
`/hook/assistant-text` endpoint, which is what lets a phone call hear the
reply. Without a hook like this wired up, the bridge never gets the text and
the caller hears silence.

Only stdlib, so nothing needs installing on top of a plain Python 3 install.

Only active for a voice session: it does nothing and exits 0 unless
`CLAUDE_VOICE_SESSION=1` is set in the environment (claude-voice.ps1 sets this,
along with `CLAUDE_SESSION_NUM`, for the terminal it launches).

Never blocks or fails the turn: short timeout, every error swallowed, always
exits 0.
"""

import json
import os
import sys
import urllib.request


def extract_last_assistant_text(transcript_path: str) -> str:
    """Read the JSONL transcript, return the last assistant turn's text."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""

    last_assistant = None
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Claude Code transcript entries usually nest the message under "message".
            inner = msg.get("message", msg)
            role = inner.get("role") or msg.get("type")
            if role == "assistant":
                last_assistant = inner

    if not last_assistant:
        return ""

    content = last_assistant.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def notify_bridge(text: str, transcript_path: str, session_num: int, base_url: str) -> None:
    """Fire-and-forget POST to the voice bridge."""
    try:
        data = json.dumps(
            {"text": text, "transcript_path": transcript_path, "session_num": session_num}
        ).encode()
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/hook/assistant-text",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2.0)
    except Exception:
        # The bridge may not be running, or the call may already have ended.
        # This must never break the Claude Code turn, so every failure is swallowed.
        pass


def main() -> int:
    if os.environ.get("CLAUDE_VOICE_SESSION") != "1":
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    transcript_path = payload.get("transcript_path", "")
    text = extract_last_assistant_text(transcript_path)
    if not text:
        return 0

    session_num = int(os.environ.get("CLAUDE_SESSION_NUM", "0") or 0)
    base_url = os.environ.get("VOICE_BRIDGE_URL", "http://localhost:8000")

    notify_bridge(text, transcript_path, session_num, base_url)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Belt and suspenders: a hook that raises can show up as a Claude Code
        # error, so nothing here is allowed to escape.
        sys.exit(0)
