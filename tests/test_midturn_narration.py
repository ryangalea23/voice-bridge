"""Text Claude writes mid-turn has to reach the phone, not wait for the turn to end.

The whole narration plan rests on this: Claude says "let me check the forecast",
then calls a tool, then answers. If only the final message were spoken, the
caller would hear the narration late or not at all.

The watcher polls the transcript every 0.15s and speaks each assistant text
block as it lands, so these tests append blocks one at a time with the turn
still in flight and check each one is spoken before the turn ends.
"""
import asyncio
import json

import bridge


def _assistant_line(text: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}]}}) + "\n"


def _tool_use_line(tool: str) -> str:
    """An assistant turn step with no text, the way a tool call lands."""
    return json.dumps({"type": "assistant",
                       "message": {"role": "assistant",
                                   "content": [{"type": "tool_use", "id": "t1",
                                                "name": tool, "input": {}}]}}) + "\n"


def test_narration_is_spoken_while_the_turn_is_still_running(tmp_path, monkeypatch):
    spoken: list[tuple[str, bool]] = []

    async def fake_speak(text):
        # Record whether the turn was still in flight when this was spoken.
        spoken.append((text, bridge._turn_active))

    monkeypatch.setattr(bridge, "_speak_content", fake_speak)
    monkeypatch.setattr(bridge, "_should_speak_reply", lambda text: True)

    path = tmp_path / "transcript.jsonl"
    path.write_bytes(b"")

    async def scenario():
        bridge._call.active = True
        bridge._turn_active = True
        task = asyncio.create_task(bridge._watch_transcript(str(path), 0))
        try:
            await asyncio.sleep(0.05)

            # 1. The opening acknowledgement, before any tool runs.
            with open(path, "ab") as f:
                f.write(_assistant_line("Sure, let me look that up.").encode("utf-8"))
            await asyncio.sleep(0.4)

            # 2. A narration line, then the tool call it belongs to.
            with open(path, "ab") as f:
                f.write(_assistant_line("Let me check the forecast.").encode("utf-8"))
                f.write(_tool_use_line("WebSearch").encode("utf-8"))
            await asyncio.sleep(0.4)

            # 3. The real answer ends the turn.
            with open(path, "ab") as f:
                f.write(_assistant_line("It is 68 and sunny in Springfield.").encode("utf-8"))
            await asyncio.sleep(0.4)
            bridge._turn_active = False
            await asyncio.sleep(0.2)
        finally:
            bridge._call.active = False
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    bridge._call.reset()
    bridge._turn_active = False

    texts = [t for t, _ in spoken]
    assert texts == [
        "Sure, let me look that up.",
        "Let me check the forecast.",
        "It is 68 and sunny in Springfield.",
    ]
    # The first two were spoken while the turn was still running, which is the
    # claim being tested. Only the last one lands after the turn is done.
    assert [mid for _, mid in spoken] == [True, True, True]


def test_a_tool_use_block_alone_is_not_spoken(tmp_path, monkeypatch):
    """Only text blocks get spoken, so a tool call does not read out its JSON."""
    spoken: list[str] = []

    async def fake_speak(text):
        spoken.append(text)

    monkeypatch.setattr(bridge, "_speak_content", fake_speak)
    monkeypatch.setattr(bridge, "_should_speak_reply", lambda text: True)

    path = tmp_path / "transcript.jsonl"
    path.write_bytes(b"")

    async def scenario():
        bridge._call.active = True
        task = asyncio.create_task(bridge._watch_transcript(str(path), 0))
        try:
            await asyncio.sleep(0.05)
            with open(path, "ab") as f:
                f.write(_tool_use_line("Read").encode("utf-8"))
            await asyncio.sleep(0.4)
        finally:
            bridge._call.active = False
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    bridge._call.reset()
    assert spoken == []
