"""The watcher must never speak a reply twice.

On a real call it read out a message from earlier in the conversation. The cause
was arithmetic, not logic: os.path.getsize counts BYTES, while reading the file
in text mode returns CHARACTERS. One emoji or curly quote in a transcript makes
those two disagree, so the next seek landed BEFORE the end of what had already
been read, and old assistant messages came round again.

These tests drive the watcher against a real file on disk, with speech captured.
"""
import asyncio
import json

import bridge


def _assistant_line(text: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]}}) + "\n"


def _run_watcher(tmp_path, monkeypatch, lines_before, lines_after, multibyte=""):
    """Write some history, start the watcher past it, append more, collect speech."""
    spoken: list[str] = []

    async def fake_speak(text):
        spoken.append(text)

    monkeypatch.setattr(bridge, "_speak_content", fake_speak)
    monkeypatch.setattr(bridge, "_should_speak_reply", lambda text: True)

    path = tmp_path / "transcript.jsonl"
    history = "".join(_assistant_line(t + multibyte) for t in lines_before)
    path.write_bytes(history.encode("utf-8"))
    start = path.stat().st_size          # bytes, exactly what bridge.py uses

    async def scenario():
        bridge._call.active = True
        task = asyncio.create_task(bridge._watch_transcript(str(path), start))
        await asyncio.sleep(0.05)
        with open(path, "ab") as f:
            f.write("".join(_assistant_line(t) for t in lines_after).encode("utf-8"))
        await asyncio.sleep(0.6)
        bridge._call.active = False
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    bridge._call.reset()
    return spoken


def test_plain_ascii_history_is_not_reread(tmp_path, monkeypatch):
    spoken = _run_watcher(tmp_path, monkeypatch,
                          ["an old answer from earlier"], ["the new answer"])
    assert spoken == ["the new answer"]


def test_multibyte_history_is_not_reread(tmp_path, monkeypatch):
    """The real failure. A curly quote and an emoji make bytes and characters
    disagree, and the old text came back out of the phone."""
    spoken = _run_watcher(
        tmp_path, monkeypatch,
        ["an old answer from earlier"], ["the new answer"],
        multibyte=" ’ curly quote and an emoji \U0001F600 and an arrow →",
    )
    assert spoken == ["the new answer"], f"old text spoken again: {spoken}"


def test_several_multibyte_appends_stay_aligned(tmp_path, monkeypatch):
    """Drift accumulates, so check more than one append."""
    spoken = _run_watcher(
        tmp_path, monkeypatch,
        ["history one \U0001F600", "history two →"],
        ["first new ’", "second new \U0001F600", "third new"],
    )
    assert spoken == ["first new ’", "second new \U0001F600", "third new"]
