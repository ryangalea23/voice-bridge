"""Cutting the bridge off has to stop the work, and the answer to a turn that
was cut off must never be read out afterwards.

All four of these came off one real call:

  21:20:39  the caller said "stop" (heard as "top"), the audio stopped
  21:20:42  the finished answer arrived and was spoken from the beginning
  20:54:16  an outbound call hit voicemail and the greeting was typed in

TTS, injection and Escape are all mocked. Nothing here touches a phone.
"""
import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import bridge
from voice_settings import DEFAULT_STOP_WORDS, load_settings


class Harness:
    """Drives _on_utterance and the two reply paths with a fake clock."""

    def __init__(self, monkeypatch, barge_in_stops_claude=True, escape_ok=True):
        self.spoken: list[str] = []
        self.injected: list[str] = []
        self.escapes = 0
        self.cleared = 0
        self.t = 1000.0
        self.escape_ok = escape_ok

        monkeypatch.setattr(bridge, "SETTINGS", replace(
            bridge.SETTINGS,
            ack_mode="off",
            barge_in_stops_claude=barge_in_stops_claude,
        ))
        monkeypatch.setattr(bridge, "TYPING_SOUND", False)
        monkeypatch.setattr(bridge, "_transcript_path", "")
        monkeypatch.setattr(bridge, "_watcher_spoke_this_turn", False)
        monkeypatch.setattr(bridge, "_now", lambda: self.t)

        async def speak(text):
            self.spoken.append(text)
        monkeypatch.setattr(bridge, "_speak_content", speak)

        async def clear_audio():
            self.cleared += 1
        monkeypatch.setattr(bridge, "_clear_audio", clear_audio)

        def inject(text):
            self.injected.append(text)
            return True
        monkeypatch.setattr(bridge, "inject_prompt", inject)

        def escape():
            self.escapes += 1
            return self.escape_ok
        monkeypatch.setattr(bridge, "send_escape", escape)

        bridge._call.reset()
        bridge._call.active = True

    # The bridge only calls _barge_in while its own audio is on the line, so the
    # tests that need a barge-in say so here rather than faking playback bytes.
    def speaking(self):
        bridge._call.speaking = True
        bridge._call.speak_since = self.t
        # The echo guard is what tells _on_utterance the bridge's own voice is on
        # the line, and that is the only state in which it barges in.
        bridge._playback_until = self.t + 5.0

    def advance(self, seconds):
        self.t += seconds

    async def _settle(self):
        for _ in range(3):
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def run(self, coro_fn):
        async def go():
            bridge._call.outbound_q = asyncio.Queue()
            await coro_fn()
            await self._settle()
        asyncio.run(go())

    # ── things a real call does ───────────────────────────────────────────────

    async def turn(self, text):
        """The caller says something that becomes a prompt."""
        await bridge._on_utterance(text)
        await self._settle()

    async def reply(self, text):
        """Claude's finished answer arriving at /hook/assistant-text."""
        spoke = bridge._should_speak_reply(text)
        bridge._end_reply_stream()
        if spoke:
            await bridge._speak_content(text)
        return spoke


def _cleanup():
    bridge._call.reset()
    bridge._stop_typing()


# ── FIX 1: talking over the bridge stops the work, not just the audio ─────────

def test_barge_in_sends_escape_when_setting_on(monkeypatch):
    h = Harness(monkeypatch, barge_in_stops_claude=True)

    async def script():
        await h.turn("run the tests")
        h.speaking()
        await h.turn("actually never mind that, what time is it")

    h.run(script)
    _cleanup()
    assert h.escapes == 1, "cutting in should press Escape"
    assert h.cleared >= 1, "the audio must stop too"
    assert h.injected[-1] == "actually never mind that, what time is it"


def test_barge_in_leaves_claude_alone_when_setting_off(monkeypatch):
    h = Harness(monkeypatch, barge_in_stops_claude=False)

    async def script():
        await h.turn("run the tests")
        h.speaking()
        await h.turn("actually never mind that, what time is it")

    h.run(script)
    _cleanup()
    assert h.escapes == 0, "BARGE_IN_STOPS_CLAUDE=off must not press Escape"
    assert h.cleared >= 1, "the audio still has to stop"


def test_barge_in_clears_audio_before_the_escape_runs(monkeypatch):
    """Escape reaches into a console window and can be slow. The audio must
    already be quiet by then."""
    h = Harness(monkeypatch, barge_in_stops_claude=True)
    order: list[str] = []

    async def clear_audio():
        order.append("clear")
    monkeypatch.setattr(bridge, "_clear_audio", clear_audio)

    def slow_escape():
        order.append("escape")
        return True
    monkeypatch.setattr(bridge, "send_escape", slow_escape)

    async def script():
        await h.turn("run the tests")
        h.speaking()
        await bridge._barge_in("caller spoke over us")
        assert order == ["clear"], "the Escape must not hold up the clear"
        await h._settle()

    h.run(script)
    _cleanup()
    assert order == ["clear", "escape"]


def test_barge_in_with_no_turn_running_does_not_escape(monkeypatch):
    """Talking over the greeting is not an interruption of any work."""
    h = Harness(monkeypatch, barge_in_stops_claude=True)

    async def script():
        h.speaking()
        await bridge._barge_in("caller spoke over the greeting")

    h.run(script)
    _cleanup()
    assert h.escapes == 0
    assert bridge._pending_drops == []


# ── FIX 2: the reply to an interrupted turn is never spoken ───────────────────

def test_reply_to_interrupted_turn_is_dropped(monkeypatch):
    h = Harness(monkeypatch, barge_in_stops_claude=False)

    async def script():
        await h.turn("how many unread emails do i have")
        h.speaking()
        await bridge._barge_in("caller spoke over us")
        spoke = await h.reply("You have 14 unread emails, 3 of them since lunch.")
        assert spoke is False

    h.run(script)
    _cleanup()
    assert h.spoken == [], "the cut-off answer must stay unspoken"


def test_reply_to_a_later_turn_is_still_spoken(monkeypatch):
    """The caller cuts in and says the next thing straight away. The old answer
    is dropped, the new one is spoken."""
    h = Harness(monkeypatch, barge_in_stops_claude=False)

    async def script():
        await h.turn("how many unread emails do i have")
        h.speaking()
        await h.turn("what time is my next meeting")
        assert await h.reply("You have 14 unread emails.") is False
        assert await h.reply("Your next meeting is at two.") is True

    h.run(script)
    _cleanup()
    assert h.spoken == ["Your next meeting is at two."]


def test_reply_without_any_interruption_is_spoken(monkeypatch):
    h = Harness(monkeypatch)

    async def script():
        await h.turn("how many unread emails do i have")
        assert await h.reply("You have 14 unread emails.") is True

    h.run(script)
    _cleanup()
    assert h.spoken == ["You have 14 unread emails."]


def test_stopped_turn_that_never_replies_does_not_eat_the_next_answer(monkeypatch):
    """Escape usually kills the turn outright. If nothing comes back inside the
    grace window the bridge stops waiting, so a later answer is still spoken."""
    h = Harness(monkeypatch, barge_in_stops_claude=True)

    async def script():
        await h.turn("run the whole suite")
        h.speaking()
        await h.turn("what time is it")
        h.advance(bridge._ESCAPED_DROP_GRACE_S + 1)
        assert await h.reply("It is half past four.") is True

    h.run(script)
    _cleanup()
    assert h.spoken == ["It is half past four."]


def test_every_block_of_one_interrupted_reply_is_dropped(monkeypatch):
    """A long answer reaches the watcher in pieces. One interruption drops the
    whole reply, not just its first piece."""
    h = Harness(monkeypatch, barge_in_stops_claude=False)

    async def script():
        await h.turn("summarise the plan")
        h.speaking()
        await bridge._barge_in("caller spoke over us")
        assert bridge._should_speak_reply("First, the plan says") is False
        assert bridge._should_speak_reply("Second, the plan says") is False

    h.run(script)
    _cleanup()


def test_stop_word_also_drops_the_late_reply(monkeypatch):
    """The exact call this came from: "stop" at 21:20:39, answer at 21:20:42."""
    h = Harness(monkeypatch)

    async def script():
        await h.turn("read the config file")
        h.speaking()
        await h.turn("stop")
        assert await h.reply("The config file sets the port to 8000.") is False

    h.run(script)
    _cleanup()
    assert h.spoken == ["Stopped."]


# ── FIX 3: clipped stop words ────────────────────────────────────────────────

@pytest.mark.parametrize("word", ["top", "op", "Top.", " OP ", "nevermind"])
def test_clipped_stop_words_press_escape_and_are_not_injected(monkeypatch, word):
    h = Harness(monkeypatch)

    async def script():
        await h.turn(word)

    h.run(script)
    _cleanup()
    assert h.injected == [], f"{word!r} must not be typed as a prompt"
    assert h.escapes == 1
    assert h.spoken == ["Stopped."]


@pytest.mark.parametrize("text", [
    "top of the file", "go to the top", "never mind the tests", "stop the server",
])
def test_sentences_around_a_clipped_stop_word_are_injected(monkeypatch, text):
    h = Harness(monkeypatch)

    async def script():
        await h.turn(text)

    h.run(script)
    _cleanup()
    assert h.injected == [text]
    assert h.escapes == 0


@pytest.mark.parametrize("word", ["stop", "cancel", "wait", "hold on", "never mind"])
def test_the_real_stop_words_still_work(monkeypatch, word):
    h = Harness(monkeypatch)

    async def script():
        await h.turn(word)

    h.run(script)
    _cleanup()
    assert h.escapes == 1
    assert h.injected == []


def test_aliases_only_count_for_the_words_in_use():
    """Custom stop words get their own aliases, and nobody else's."""
    s = load_settings({"VOICE_STOP_WORDS": "cancel"})
    assert "top" not in s.stop_aliases, "no stop word 'stop', so no 'top'"
    assert "ancel" in s.stop_aliases


def test_aliases_can_be_turned_off():
    s = load_settings({"VOICE_STOP_ALIASES": ""})
    assert s.stop_aliases == ()
    assert s.stop_words == DEFAULT_STOP_WORDS


def test_aliases_can_be_replaced():
    s = load_settings({"VOICE_STOP_WORDS": "stop", "VOICE_STOP_ALIASES": "stop=shtop|sop"})
    assert s.stop_aliases == ("shtop", "sop")


def test_default_aliases_cover_the_clipped_stop(monkeypatch):
    s = load_settings({})
    assert "top" in s.stop_aliases and "op" in s.stop_aliases


def test_barge_in_setting_reads_on_and_off():
    assert load_settings({}).barge_in_stops_claude is True
    assert load_settings({"BARGE_IN_STOPS_CLAUDE": "off"}).barge_in_stops_claude is False
    assert load_settings({"BARGE_IN_STOPS_CLAUDE": "ON"}).barge_in_stops_claude is True


def test_bad_barge_in_setting_falls_back_and_logs(caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        s = load_settings({"BARGE_IN_STOPS_CLAUDE": "maybe"})
    assert s.barge_in_stops_claude is True
    assert "BARGE_IN_STOPS_CLAUDE" in caplog.text


# ── FIX 4: an outbound call must not listen to voicemail ──────────────────────

client = TestClient(bridge.app)
FORM = {"content-type": "application/x-www-form-urlencoded"}
ALLOWED = "%2B15551112222"


def test_outbound_answered_by_machine_hangs_up():
    r = client.post(
        "/twilio/voice",
        content=f"From=%2B15559990000&To={ALLOWED}&Direction=outbound-api"
                "&AnsweredBy=machine_start&CallSid=CA10",
        headers=FORM,
    )
    assert r.status_code == 200
    assert "<Hangup/>" in r.text
    assert "<Stream" not in r.text, "no stream means nothing is transcribed or typed"


@pytest.mark.parametrize("answered_by", [
    "machine_start", "machine_end_beep", "machine_end_silence",
    "machine_end_other", "fax",
])
def test_every_machine_answer_hangs_up(answered_by):
    r = client.post(
        "/twilio/voice",
        content=f"From=%2B15559990000&To={ALLOWED}&Direction=outbound-api"
                f"&AnsweredBy={answered_by}&CallSid=CA11",
        headers=FORM,
    )
    assert "<Stream" not in r.text and "<Hangup/>" in r.text


@pytest.mark.parametrize("answered_by", ["human", "unknown", ""])
def test_outbound_answered_by_a_person_gets_the_stream(answered_by):
    r = client.post(
        "/twilio/voice",
        content=f"From=%2B15559990000&To={ALLOWED}&Direction=outbound-api"
                f"&AnsweredBy={answered_by}&CallSid=CA12",
        headers=FORM,
    )
    assert "<Stream" in r.text and 'name="ticket"' in r.text


def test_outbound_to_a_number_not_on_the_list_is_rejected():
    r = client.post(
        "/twilio/voice",
        content="From=%2B15559990000&To=%2B19998887777&Direction=outbound-api"
                "&AnsweredBy=human&CallSid=CA13",
        headers=FORM,
    )
    assert "not authorized" in r.text and "<Stream" not in r.text


# An inbound call is the path that has to stay exactly as it was.

def test_inbound_call_is_unchanged_by_the_machine_detection():
    r = client.post(
        "/twilio/voice", content=f"From={ALLOWED}&CallSid=CA14", headers=FORM
    )
    assert "<Stream" in r.text and 'name="ticket"' in r.text


def test_inbound_call_ignores_answered_by_entirely():
    """Twilio never sends AnsweredBy on an inbound call. If something does, it
    must not be able to hang up a real call to the number."""
    r = client.post(
        "/twilio/voice",
        content=f"From={ALLOWED}&Direction=inbound&AnsweredBy=machine_start&CallSid=CA15",
        headers=FORM,
    )
    assert "<Stream" in r.text, "an inbound call is never subject to machine detection"


def test_inbound_call_still_checks_the_caller_not_the_dialled_number():
    r = client.post(
        "/twilio/voice",
        content=f"From=%2B19998887777&To={ALLOWED}&Direction=inbound&CallSid=CA16",
        headers=FORM,
    )
    assert "not authorized" in r.text and "<Stream" not in r.text
