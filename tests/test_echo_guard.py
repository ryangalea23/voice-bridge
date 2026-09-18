"""The bridge must never treat its own voice as caller speech.

On a real call the phone speaker played "I heard: how many unread emails do i
have" back into the phone mic, Deepgram transcribed "i heard", the bridge
injected that and spoke another read-back, forever. These tests drive the same
path with a fake clock, fake TTS audio and injection mocked.
"""
import asyncio
import logging
from dataclasses import replace

import pytest

import bridge
from voice_text import is_echo_of


class Clock:
    """Stands in for the event loop clock so the guard window is exact."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Harness:
    """Real _speak_content and real echo guard; only TTS, injection and the
    clock are faked."""

    def __init__(self, monkeypatch, speech_seconds=2.0, mode="transcript"):
        self.clock = Clock()
        self.injected: list[str] = []
        self.escapes = 0
        self.spoke: list[str] = []
        self.speech_seconds = speech_seconds

        monkeypatch.setattr(bridge, "SETTINGS", replace(bridge.SETTINGS, readback=mode))
        monkeypatch.setattr(bridge, "TYPING_SOUND", False)
        monkeypatch.setattr(bridge, "_transcript_path", "")
        monkeypatch.setattr(bridge, "_now", self.clock)
        monkeypatch.setattr(bridge, "ECHO_GUARD_MS", 1200)

        async def fake_tts(text):
            self.spoke.append(text)
            # One chunk whose length is exactly the intended play time.
            yield b"\xff" * int(bridge.MULAW_BYTES_PER_SECOND * self.speech_seconds)

        monkeypatch.setattr(bridge, "text_to_mulaw_chunks", fake_tts)

        def inject(text):
            self.injected.append(text)
            return True

        monkeypatch.setattr(bridge, "inject_prompt", inject)

        def escape():
            self.escapes += 1
            return True

        monkeypatch.setattr(bridge, "send_escape", escape)

        bridge._call.reset()
        bridge._call.active = True

    async def speak(self, text):
        await bridge._speak_content(text)

    async def utter(self, text):
        await bridge._on_utterance(text)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def run(self, coro_fn):
        async def go():
            bridge._call.outbound_q = asyncio.Queue()
            await coro_fn()

        asyncio.run(go())
        bridge._call.reset()
        bridge._stop_typing()


# ── the guard window ───────────────────────────────────────────────────────────

def test_utterance_while_tts_playing_is_dropped(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="bridge")
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Here are your three unread emails.")
        h.clock.advance(0.5)          # still inside the 2s of audio
        await h.utter("some new request")

    h.run(scenario)
    assert h.injected == []
    assert "Echo guard dropped" in caplog.text
    assert "some new request" in caplog.text


def test_utterance_inside_guard_tail_is_dropped(monkeypatch):
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Here are your three unread emails.")
        h.clock.advance(2.0 + 1.0)    # audio done, 1.0s into the 1.2s tail
        await h.utter("some new request")

    h.run(scenario)
    assert h.injected == []


def test_same_utterance_after_the_tail_is_injected(monkeypatch):
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Here are your three unread emails.")
        h.clock.advance(2.0 + 1.3)    # past the 1.2s tail
        await h.utter("some new request")

    h.run(scenario)
    assert h.injected == ["some new request"]


def test_stop_word_during_tts_still_escapes(monkeypatch):
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Here are your three unread emails.")
        h.clock.advance(0.5)
        await h.utter("stop")

    h.run(scenario)
    assert h.escapes == 1
    assert h.injected == []
    assert "Stopped." in h.spoke


# ── the last-spoken-text net ───────────────────────────────────────────────────

def test_echo_of_last_spoken_dropped_outside_the_guard(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="bridge")
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("I heard: how many unread emails do i have")
        h.clock.advance(60.0)         # long past any guard window
        await h.utter("I heard, how many unread emails do I have.")

    h.run(scenario)
    assert h.injected == []
    assert "repeats what the bridge just said" in caplog.text


def test_prefix_of_last_spoken_dropped_outside_the_guard(monkeypatch):
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("I heard: how many unread emails do i have")
        h.clock.advance(60.0)
        await h.utter("i heard how many unread")

    h.run(scenario)
    assert h.injected == []


def test_real_request_starting_with_i_heard_is_injected(monkeypatch):
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("I heard: how many unread emails do i have")
        h.clock.advance(60.0)
        await h.utter("I heard the build is broken, can you check it")

    h.run(scenario)
    assert h.injected == ["I heard the build is broken, can you check it"]


def test_short_utterance_is_not_treated_as_echo(monkeypatch):
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Yes, the tests pass.")
        h.clock.advance(60.0)
        await h.utter("yes")

    h.run(scenario)
    assert h.injected == ["yes"]


# ── the observed loop ──────────────────────────────────────────────────────────

def test_observed_readback_loop_stops_dead(monkeypatch):
    """Speak the read-back, then feed the read-back text straight back in, the
    way the phone speaker did. Nothing may be injected, nothing new spoken."""
    h = Harness(monkeypatch)

    async def scenario():
        await h.utter("how many unread emails do i have")
        assert h.injected == ["how many unread emails do i have"]
        spoken_before = list(h.spoke)
        assert spoken_before, "the read-back should have been spoken"

        # 5 seconds later, exactly as the log showed.
        h.clock.advance(5.0)
        await h.utter("i heard")
        await h.utter(spoken_before[-1])

        assert h.injected == ["how many unread emails do i have"]
        assert h.spoke == spoken_before

    h.run(scenario)


# ── barge-in ───────────────────────────────────────────────────────────────────

def test_barge_in_ignored_while_the_bridge_speaks(monkeypatch):
    h = Harness(monkeypatch)
    cleared = []
    interrupted = []

    async def fake_clear():
        cleared.append(True)

    monkeypatch.setattr(bridge, "_clear_audio", fake_clear)

    after_speaking = []

    async def scenario():
        await h.speak("A long answer that is still playing.")
        # Speaking clears Twilio's buffer on purpose, so count only what the
        # barge-in itself adds.
        after_speaking.append(len(cleared))
        h.clock.advance(0.5)
        await bridge._on_speech_started()
        interrupted.append(bridge._call.interrupted)

    h.run(scenario)
    assert len(cleared) - after_speaking[0] == 0
    assert interrupted == [False]


def test_barge_in_still_works_when_no_audio_is_playing(monkeypatch):
    h = Harness(monkeypatch)
    cleared = []
    interrupted = []

    async def fake_clear():
        cleared.append(True)

    monkeypatch.setattr(bridge, "_clear_audio", fake_clear)

    after_speaking = []

    async def scenario():
        await h.speak("Short answer.")
        after_speaking.append(len(cleared))   # speaking clears on purpose
        h.clock.advance(2.0 + 1.3)     # audio and tail both over
        # _on_speech_started uses the loop clock for its own 3s rule; make the
        # speech look old enough that the rule is satisfied.
        bridge._call.speak_since = asyncio.get_event_loop().time() - 10.0
        await bridge._on_speech_started()
        interrupted.append(bridge._call.interrupted)

    h.run(scenario)
    assert len(cleared) - after_speaking[0] == 1
    assert interrupted == [True]


# ── the text comparison on its own ─────────────────────────────────────────────

@pytest.mark.parametrize("heard,said,expected", [
    ("I heard: run the tests", "I heard: run the tests", True),
    ("i heard run the", "I heard: run the tests", True),
    ("run the tests please", "I heard: run the tests", False),
    ("anything at all", "", False),
    ("yes", "Yes, the tests pass.", False),
    ("i heard", "I heard: how many unread emails do i have", True),
])
def test_is_echo_of(heard, said, expected):
    assert is_echo_of(heard, said) is expected
