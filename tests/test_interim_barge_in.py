"""Interrupting has to work while the caller is still talking.

From a real call:

  22:06:40,263  STT interim: 'no'
  22:06:40,263  Speech started while our audio plays - deciding at the transcript
  22:06:40,984  STT interim: "no i it's"
  22:06:41,943  STT interim: 'no i told you not to'
  22:06:43,044  STT interim: "no i told you not to stop there's still"
  22:06:43,934  STT interim: "no i told you not to stop you're still talking"

The bridge kept talking for nearly four seconds. Barge-in was decided only on a
finished utterance, and a finished utterance needs UTTERANCE_END_MS of silence,
which never comes while the caller is mid-sentence. These tests drive the
partial-transcript path instead.

TTS, injection and Escape are all mocked. Nothing here touches a phone.
"""
import asyncio
from dataclasses import replace

import bridge
from stt import DeepgramSTT
from voice_settings import load_settings


class Harness:
    """Drives _on_interim and _on_utterance with a fake clock."""

    def __init__(self, monkeypatch, on_interim=True, barge_in_stops_claude=True,
                 clear_stops_playback=True):
        self.spoken: list[str] = []
        self.injected: list[str] = []
        self.escapes = 0
        self.cleared = 0
        self.t = 1000.0

        monkeypatch.setattr(bridge, "SETTINGS", replace(
            bridge.SETTINGS,
            ack_mode="off",
            barge_in_stops_claude=barge_in_stops_claude,
            barge_in_on_interim=on_interim,
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
            # The real _clear_audio drains the outbound queue, which stops the
            # playback clock. Without that here, the bridge would still think
            # its own audio was on the line after a barge-in.
            if clear_stops_playback:
                bridge._reset_playback_clock()
        monkeypatch.setattr(bridge, "_clear_audio", clear_audio)

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
        self.speaking_at_end = None

    # ── call states ───────────────────────────────────────────────────────────

    def speaking(self, text="Here are the three files that changed today."):
        """Our audio is on the line, and this is the sentence being played."""
        bridge._call.speaking = True
        bridge._call.speak_since = self.t
        bridge._playback_until = self.t + 5.0
        bridge._remember_spoken(text)

    def silent(self):
        bridge._playback_until = 0.0

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
            # _cleanup() resets the call, so record this before it does.
            self.speaking_at_end = bridge._call.speaking
        asyncio.run(go())

    # ── things a real call does ───────────────────────────────────────────────

    async def interim(self, text):
        await bridge._on_interim(text)
        await self._settle()

    async def turn(self, text):
        await bridge._on_utterance(text)
        await self._settle()


def _cleanup():
    bridge._call.reset()
    bridge._stop_typing()


# ── the caller cutting in ─────────────────────────────────────────────────────

def test_interim_that_is_not_echo_barges_in_at_once(monkeypatch):
    h = Harness(monkeypatch)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        await h.interim("no i told you")

    h.run(script)
    _cleanup()
    assert h.cleared >= 1, "the audio has to stop on the partial transcript"
    assert h.escapes == 1, "cutting in should press Escape"
    assert h.speaking_at_end is False, "the sentence in flight is cancelled"
    assert h.injected == ["summarise today's changes"], "interim text is never injected"


def test_interim_that_is_our_own_voice_does_not_barge_in(monkeypatch):
    h = Harness(monkeypatch)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking("Here are the three files that changed today.")
        # The phone speaker feeding our own sentence back into the mic.
        await h.interim("the three files that changed")

    h.run(script)
    _cleanup()
    assert h.cleared == 0, "our own voice must not stop our own sentence"
    assert h.escapes == 0
    assert h.speaking_at_end is True


def test_growing_interims_barge_in_exactly_once(monkeypatch):
    h = Harness(monkeypatch)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        for partial in (
            "no i told you",
            "no i told you not to",
            "no i told you not to stop",
            "no i told you not to stop you're still talking",
        ):
            await h.interim(partial)

    h.run(script)
    _cleanup()
    assert h.cleared == 1, "one barge-in, not one per word"
    assert h.escapes == 1, "Escape goes out at most once per sentence"


def test_only_one_escape_even_if_audio_is_still_playing(monkeypatch):
    """The clear is not always the end of it: a tool phrase queued a moment later
    puts our audio back on the line. The rest of the caller's sentence must not
    press Escape a second time, which would stop the turn they just started."""
    h = Harness(monkeypatch, clear_stops_playback=False)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        for partial in ("no i told you", "no i told you not to", "no i told you not to stop"):
            await h.interim(partial)

    h.run(script)
    _cleanup()
    assert h.cleared == 1, "one barge-in per sentence, however long our audio runs"
    assert h.escapes == 1, "Escape must not go out once per word"


def test_a_new_sentence_can_barge_in_again_after_the_final_lands(monkeypatch):
    h = Harness(monkeypatch)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        await h.interim("no i told you")
        # The finished utterance arrives and is injected as a new turn.
        await h.turn("no i told you not to")
        # The bridge answers, and the caller cuts in again.
        h.speaking("Sorry about that. The three files are bridge, stt and tts.")
        await h.interim("actually forget the files")

    h.run(script)
    _cleanup()
    assert h.cleared == 2, "the second sentence gets its own barge-in"
    assert h.escapes == 2


def test_the_final_utterance_is_still_injected_after_a_barge_in(monkeypatch):
    h = Harness(monkeypatch)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        await h.interim("no i told you")
        await h.turn("no i told you not to stop")

    h.run(script)
    _cleanup()
    assert h.injected == [
        "summarise today's changes",
        "no i told you not to stop",
    ], "the finished sentence still becomes a prompt"


def test_replaying_the_real_call_barges_in_within_the_first_two_interims(monkeypatch):
    """The log above, with its real timings. The barge-in must land inside the
    first second, not after four."""
    h = Harness(monkeypatch)
    log = [
        (0.000, "no"),
        (0.721, "no i it's"),
        (1.680, "no i told you not to"),
        (2.781, "no i told you not to stop there's still"),
        (3.671, "no i told you not to stop you're still talking"),
    ]
    barged_at: list[float] = []

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        start = h.t
        for offset, partial in log:
            h.t = start + offset
            before = h.cleared
            await h.interim(partial)
            if h.cleared > before:
                barged_at.append(offset)

    h.run(script)
    _cleanup()
    assert barged_at, "the caller talking over us has to stop the audio"
    assert barged_at[0] <= 0.8, f"barge-in was {barged_at[0]:.3f}s late"
    assert len(barged_at) == 1
    assert h.escapes == 1


# ── when the interim path must do nothing ─────────────────────────────────────

def test_interims_with_no_audio_playing_change_nothing(monkeypatch):
    h = Harness(monkeypatch)

    async def script():
        await h.turn("summarise today's changes")
        h.silent()
        await h.interim("no i told you not to stop")

    h.run(script)
    _cleanup()
    assert h.cleared == 0, "there is nothing to interrupt when we are not talking"
    assert h.escapes == 0


def test_setting_off_matches_the_old_behaviour(monkeypatch):
    h = Harness(monkeypatch, on_interim=False)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        for partial in ("no", "no i told you", "no i told you not to stop"):
            await h.interim(partial)

    h.run(script)
    _cleanup()
    assert h.cleared == 0, "BARGE_IN_ON_INTERIM=off waits for the finished utterance"
    assert h.escapes == 0
    assert h.speaking_at_end is True


def test_a_one_word_interim_waits_for_the_next_one(monkeypatch):
    """Under the echo floor the bridge cannot tell a scrap of our own sentence
    from the caller, so it holds off - for a few hundred milliseconds, not four
    seconds."""
    h = Harness(monkeypatch)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        await h.interim("no")

    h.run(script)
    _cleanup()
    assert h.cleared == 0
    assert h.escapes == 0


def test_a_stop_word_interim_leaves_escape_to_the_utterance_path(monkeypatch):
    """"hold on" clears the two-word floor, but the finished utterance already
    presses Escape. Doing it here too would send Escape twice."""
    h = Harness(monkeypatch)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        await h.interim("hold on")
        await h.turn("hold on")

    h.run(script)
    _cleanup()
    assert h.escapes == 1, "one Escape for one stop word"
    assert h.injected == ["summarise today's changes"], "a stop word is not a prompt"


def test_barge_in_stops_claude_off_still_stops_the_audio(monkeypatch):
    h = Harness(monkeypatch, barge_in_stops_claude=False)

    async def script():
        await h.turn("summarise today's changes")
        h.speaking()
        await h.interim("no i told you")

    h.run(script)
    _cleanup()
    assert h.cleared == 1, "the audio still has to stop"
    assert h.escapes == 0, "BARGE_IN_STOPS_CLAUDE=off must not press Escape"


# ── the setting ───────────────────────────────────────────────────────────────

def test_barge_in_on_interim_defaults_to_on():
    assert load_settings({}).barge_in_on_interim is True


def test_barge_in_on_interim_can_be_turned_off():
    assert load_settings({"BARGE_IN_ON_INTERIM": "off"}).barge_in_on_interim is False


def test_a_junk_barge_in_on_interim_value_falls_back_to_on():
    assert load_settings({"BARGE_IN_ON_INTERIM": "maybe"}).barge_in_on_interim is True


# ── stt.py hands the partial text over ────────────────────────────────────────

class _FakeAlt:
    def __init__(self, transcript, confidence=0.9):
        self.transcript = transcript
        self.confidence = confidence


class _FakeResult:
    def __init__(self, transcript, is_final, confidence=0.9):
        self.is_final = is_final
        self.channel = type("C", (), {"alternatives": [_FakeAlt(transcript, confidence)]})()


def test_stt_reports_interim_text_and_still_buffers_finals():
    seen: list[str] = []
    starts: list[int] = []

    async def on_utterance(text):
        pass

    async def on_interim(text):
        seen.append(text)

    async def on_speech_started():
        starts.append(1)

    stt = DeepgramSTT(
        on_utterance=on_utterance,
        on_speech_started=on_speech_started,
        on_interim=on_interim,
    )

    async def go():
        await stt.handle_transcript(_FakeResult("no i told", is_final=False))
        await stt.handle_transcript(_FakeResult("no i told you", is_final=False))
        await stt.handle_transcript(_FakeResult("no i told you not to", is_final=True))

    asyncio.run(go())
    assert seen == ["no i told", "no i told you"], "only partials reach on_interim"
    assert starts == [1, 1], "the old speech-started callback is untouched"
    assert stt._accumulated == "no i told you not to", "finals still buffer for UtteranceEnd"


def test_stt_without_an_interim_callback_still_works():
    got: list[str] = []

    async def on_utterance(text):
        got.append(text)

    stt = DeepgramSTT(on_utterance=on_utterance)
    asyncio.run(stt.handle_transcript(_FakeResult("hello there", is_final=False)))
    assert stt._current == "hello there"
