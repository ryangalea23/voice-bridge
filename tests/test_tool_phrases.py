"""The canned tool phrases must match the real tool names, and stay quiet by default.

Two bugs are pinned here. First, the phrase builder tested for "web_search" and
"web_fetch" with underscores. Claude Code sends WebSearch and WebFetch, so those
tests never matched and a web lookup fell through to the code-search branch: on a
real call the caller asked for the weather and heard "Searching code...".

Second, the phrases now compete with Claude narrating its own steps, so
TOOL_PHRASES is off by default and the hook has to accept the post in silence.
"""
import asyncio

import pytest

import bridge
from voice_settings import load_settings


# Every tool name Claude Code actually sends, with the input it comes with.
CASES = [
    ("WebSearch", {"query": "what is the weather today"}, "Looking up what is the weather today on the web..."),
    ("WebSearch", {}, "Looking that up on the web..."),
    ("WebFetch", {"url": "https://example.com"}, "Opening that page..."),
    ("Read", {"file_path": "C:/repo/bridge.py"}, "Reading bridge.py..."),
    ("Write", {"file_path": "C:/repo/notes.md"}, "Writing notes.md..."),
    ("Edit", {"file_path": "C:/repo/bridge.py"}, "Editing bridge.py..."),
    ("Glob", {"pattern": "**/*.py"}, "Finding **/*.py..."),
    ("Grep", {"pattern": "tool_phrases"}, "Searching for tool_phrases..."),
    ("Bash", {"command": "git status --short"}, "Running git..."),
    ("Task", {"prompt": "go look at the logs"}, "Handing that to a helper agent..."),
    ("TodoWrite", {"todos": []}, "Updating my to-do list..."),
]


@pytest.mark.parametrize("tool,inp,expected", CASES)
def test_phrase_for_each_real_tool_name(tool, inp, expected):
    assert bridge._build_tool_phrase(tool, inp) == expected


def test_web_search_never_mentions_code():
    """The exact failing case from the call: a weather question over WebSearch."""
    phrase = bridge._build_tool_phrase("WebSearch", {"query": "what is the weather today"})
    assert "code" not in phrase.lower()
    assert "web" in phrase.lower()


def test_long_query_is_cut_at_a_word():
    phrase = bridge._build_tool_phrase(
        "WebSearch", {"query": "how much snow is forecast for Westchester County tonight"}
    )
    assert phrase == "Looking up how much snow is forecast for on the web..."


def test_todo_write_is_not_heard_as_writing_a_file():
    phrase = bridge._build_tool_phrase("TodoWrite", {"todos": []})
    assert "file" not in phrase.lower()


# ── TOOL_PHRASES ──────────────────────────────────────────────────────────────


def test_tool_phrases_defaults_off():
    assert load_settings({}).tool_phrases is False
    assert load_settings({"TOOL_PHRASES": "on"}).tool_phrases is True
    assert load_settings({"TOOL_PHRASES": "off"}).tool_phrases is False


class _FakeRequest:
    """Just enough of a Starlette request for the hook endpoint."""

    class _Client:
        host = "127.0.0.1"

    client = _Client()

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _post_tool_use(monkeypatch, tool_phrases: bool):
    spoken: list[str] = []

    async def fake_speak_tool(text):
        spoken.append(text)

    monkeypatch.setattr(bridge, "_speak_tool", fake_speak_tool)
    monkeypatch.setattr(
        bridge, "SETTINGS", load_settings({"TOOL_PHRASES": "on" if tool_phrases else "off"})
    )

    async def scenario():
        bridge._call.active = True
        try:
            result = await bridge.tool_use(
                _FakeRequest({"session_num": 0, "tool": "Read",
                              "input": {"file_path": "C:/repo/bridge.py"}})
            )
            # The speak is fired as a task, so let it run.
            await asyncio.sleep(0)
            return result
        finally:
            bridge._call.active = False

    result = asyncio.run(scenario())
    return result, spoken


def test_tool_use_says_nothing_when_off(monkeypatch):
    result, spoken = _post_tool_use(monkeypatch, tool_phrases=False)
    # A dict return is a 200 from FastAPI; a Response would carry a status code.
    assert not isinstance(result, bridge.Response)
    assert result == {"status": "tool-phrases-off"}
    assert spoken == []


def test_tool_use_speaks_when_on(monkeypatch):
    result, spoken = _post_tool_use(monkeypatch, tool_phrases=True)
    assert result == {"status": "ok"}
    assert spoken == ["Reading bridge.py..."]
