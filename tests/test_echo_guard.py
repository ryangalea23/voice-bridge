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

    def __init__(self, monkeypatch, speech_seconds=2.0, mode="transcript", ack_mode="off"):
        self.clock = Clock()
        self.injected: list[str] = []
        self.escapes = 0
        self.spoke: list[str] = []
        self.speech_seconds = speech_seconds

        monkeypatch.setattr(
            bridge, "SETTINGS",
            replace(bridge.SETTINGS, readback=mode, ack_mode=ack_mode),
        )
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

def test_echo_while_tts_playing_is_dropped(monkeypatch, caplog):
    """Our own words coming back while our audio is still on the line."""
    caplog.set_level(logging.INFO, logger="bridge")
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Here are your three unread emails.")
        h.clock.advance(0.5)          # still inside the 2s of audio
        await h.utter("here are your three unread")

    h.run(scenario)
    assert h.injected == []
    assert "Echo dropped" in caplog.text


def test_echo_inside_guard_tail_is_dropped(monkeypatch):
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Here are your three unread emails.")
        h.clock.advance(2.0 + 1.0)    # audio done, 1.0s into the 1.2s tail
        await h.utter("your three unread emails")

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
    h = Harness(monkeypatch, ack_mode="readback")

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

def test_speech_started_alone_does_not_barge_in(monkeypatch):
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
    ("emails do i have", "I heard: how many unread emails do i have", True),
    ("how many unread emails", "I heard: how many unread emails do i have", True),
    ("no", "No, nothing is broken.", False),
    ("run the", "Overrun theatre seating", False),
])
def test_is_echo_of(heard, said, expected):
    assert is_echo_of(heard, said) is expected
# Barge-in versus echo, while our audio is on the line.
#
# The rule: our own words are dropped, anything else stops us talking. These
# tests drive both halves through the real _on_utterance with the guard window
# open, so a change that brings back the blanket drop fails here.

def _count_clears(monkeypatch):
    """Replace _clear_audio with a counter. _speak_content clears on purpose when
    it starts, so tests compare counts rather than assert an absolute number."""
    cleared = []

    async def fake_clear():
        cleared.append(True)

    monkeypatch.setattr(bridge, "_clear_audio", fake_clear)
    return cleared


def test_echo_while_speaking_does_not_barge_in(monkeypatch):
    """Part of our own sentence coming back must not cut the sentence off."""
    h = Harness(monkeypatch)
    cleared = _count_clears(monkeypatch)
    marks = {}

    async def scenario():
        await h.speak("Here are your three unread emails from this morning.")
        marks["clears"] = len(cleared)
        marks["version"] = bridge._content_version
        h.clock.advance(0.5)                      # our audio is still playing
        await h.utter("your three unread emails")

    h.run(scenario)
    assert h.injected == []
    assert len(cleared) == marks["clears"], "echo must not clear the audio"
    assert bridge._content_version == marks["version"], "echo must not cancel speech"


@pytest.mark.parametrize("heard", [
    "here are your three unread emails from this morning",   # the whole thing
    "here are your three unread",                            # the start
    "emails from this morning",                               # the end
    "your three unread emails from",                          # out of the middle
])
def test_echo_matches_any_run_of_the_spoken_text(monkeypatch, heard):
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Here are your three unread emails from this morning.")
        h.clock.advance(0.5)
        await h.utter(heard)

    h.run(scenario)
    assert h.injected == []


def test_different_utterance_barges_in_while_speaking(monkeypatch, caplog):
    """The regression this fixes: the caller must be able to cut in mid-answer."""
    caplog.set_level(logging.INFO, logger="bridge")
    h = Harness(monkeypatch)
    cleared = _count_clears(monkeypatch)
    marks = {}

    async def scenario():
        await h.speak("Here are your three unread emails from this morning.")
        marks["clears"] = len(cleared)
        marks["version"] = bridge._content_version
        h.clock.advance(0.5)                      # our audio is still playing
        await h.utter("actually check the deploy instead")

    h.run(scenario)
    assert h.injected == ["actually check the deploy instead"]
    assert len(cleared) - marks["clears"] >= 1, "barge-in must clear the audio"
    assert bridge._content_version > marks["version"], "barge-in must cancel speech"
    assert "Barge-in" in caplog.text


def test_barge_in_stops_the_audio_mid_sentence(monkeypatch):
    """Proof the sentence really stops: count the chunks that reach the queue.

    The fake TTS yields ten chunks with an await between them, so the utterance
    can land part-way through. Without the version bump all ten would go out.
    """
    h = Harness(monkeypatch)
    chunks_sent = []

    async def slow_tts(text):
        h.spoke.append(text)
        for i in range(10):
            await asyncio.sleep(0.005)
            chunks_sent.append(i)
            yield bytes([0xff]) * int(bridge.MULAW_BYTES_PER_SECOND * 0.5)

    monkeypatch.setattr(bridge, "text_to_mulaw_chunks", slow_tts)

    async def scenario():
        speak = asyncio.create_task(bridge._speak_content("A long answer that keeps going."))
        await asyncio.sleep(0.02)                 # a few chunks are out
        assert bridge._call.speaking
        sent_before = len(chunks_sent)
        await bridge._on_utterance("actually check the deploy instead")
        await asyncio.gather(speak, return_exceptions=True)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert sent_before < 10, "the test needs the speech still in flight"
        assert len(chunks_sent) < 10, "the rest of the sentence should never be queued"
        assert h.injected == ["actually check the deploy instead"]

    h.run(scenario)


def test_barge_in_works_inside_the_guard_tail(monkeypatch):
    """The tail covers audio Twilio still holds, so the same text test applies
    there rather than a blanket drop."""
    h = Harness(monkeypatch)
    cleared = _count_clears(monkeypatch)
    marks = {}

    async def scenario():
        await h.speak("Here are your three unread emails from this morning.")
        marks["clears"] = len(cleared)
        h.clock.advance(2.0 + 1.0)                # audio done, inside the 1.2s tail
        await h.utter("actually check the deploy instead")

    h.run(scenario)
    assert h.injected == ["actually check the deploy instead"]
    assert len(cleared) - marks["clears"] >= 1


@pytest.mark.parametrize("word", ["yes", "no", "sure", "yeah"])
def test_short_words_while_speaking_are_caller_speech(monkeypatch, word):
    """The false-positive direction. A one-word answer spoken over us is the
    caller, never echo, even when we just said that word ourselves."""
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Yes, sure, no problem, yeah I can do that.")
        h.clock.advance(0.5)                      # our audio is still playing
        await h.utter(word)

    h.run(scenario)
    assert h.injected == [word]


def test_stop_word_during_speech_escapes_and_is_not_injected(monkeypatch):
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("Here are your three unread emails from this morning.")
        h.clock.advance(0.5)
        await h.utter("stop")

    h.run(scenario)
    assert h.escapes == 1
    assert h.injected == []


def test_observed_loop_replayed_with_the_new_guard(monkeypatch):
    """The exact call that started this: the bridge speaks a read-back, the phone
    speaker plays it back, Deepgram hears "i heard". Nothing may be injected and
    nothing new may be spoken."""
    h = Harness(monkeypatch, ack_mode="readback")

    async def scenario():
        await h.speak("I heard: how many unread emails do i have")
        spoken_before = list(h.spoke)
        h.clock.advance(0.5)                      # still mid-playback
        await h.utter("i heard")
        h.clock.advance(5.0)                      # and again after the tail
        await h.utter("i heard")

    h.run(scenario)
    assert h.injected == []
    assert h.spoke == ["I heard: how many unread emails do i have"]


def test_echo_of_the_previous_sentence_is_dropped_inside_the_window(monkeypatch):
    """The mic can run a sentence behind us, so the sentence before counts too -
    but only while echo is still possible."""
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("The build finished and every test passed.")
        await h.speak("Nothing else is waiting on you.")
        h.clock.advance(0.5)                      # our audio is still playing
        await h.utter("and every test passed")

    h.run(scenario)
    assert h.injected == []


def test_old_sentence_repeated_after_the_window_is_a_real_request(monkeypatch):
    """Past the window it is just a phrase we said a while ago. The caller is
    allowed to say it back on purpose."""
    h = Harness(monkeypatch)

    async def scenario():
        await h.speak("The build finished and every test passed.")
        await h.speak("Nothing else is waiting on you.")
        h.clock.advance(60.0)
        await h.utter("and every test passed")

    h.run(scenario)
    assert h.injected == ["and every test passed"]
