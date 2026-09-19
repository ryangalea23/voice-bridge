"""_on_utterance per ACK_MODE and READBACK mode, with injection, speech and
Haiku mocked.

ACK_MODE decides whether anything is said after your words are typed in; READBACK
only picks the flavour of the read-back, and only when ACK_MODE=readback. Most of
these tests are about the read-back, so the harness defaults to ACK_MODE=readback.
"""
import asyncio
import threading
import time
from dataclasses import replace

import pytest

import bridge
import readback


class Harness:
    def __init__(self, monkeypatch, mode="transcript", haiku=None, key="key",
                 timeout_ms=2500, ack_mode="readback"):
        self.spoken: list[str] = []
        self.injected: list[tuple[str, float]] = []
        self.escapes = 0
        self.t0 = 0.0
        self.typing_started = 0
        monkeypatch.setattr(bridge, "SETTINGS", replace(
            bridge.SETTINGS, readback=mode, ack_mode=ack_mode,
            anthropic_api_key=key, haiku_timeout_ms=timeout_ms,
        ))
        monkeypatch.setattr(bridge, "TYPING_SOUND", False)
        monkeypatch.setattr(bridge, "_transcript_path", "")
        monkeypatch.setattr(bridge, "_watcher_spoke_this_turn", False)

        async def speak(text):
            self.spoken.append(text)
        monkeypatch.setattr(bridge, "_speak_content", speak)

        def inject(text):
            self.injected.append((text, time.monotonic() - self.t0))
            return True
        monkeypatch.setattr(bridge, "inject_prompt", inject)

        def escape():
            self.escapes += 1
            return True
        monkeypatch.setattr(bridge, "send_escape", escape)

        # The real _start_typing, but with a stub loop: the live one sleeps until
        # the turn ends, which would hang the test.
        async def fake_typing_loop():
            self.typing_started += 1
        monkeypatch.setattr(bridge, "_typing_loop", fake_typing_loop)
        monkeypatch.setattr(bridge, "TYPING_SOUND", True)

        if haiku is not None:
            monkeypatch.setattr(readback, "haiku_restate", haiku)

        bridge._call.reset()
        bridge._call.active = True

    def run(self, *utterances):
        async def go():
            bridge._call.outbound_q = asyncio.Queue()
            self.t0 = time.monotonic()
            for u in utterances:
                await bridge._on_utterance(u)
                pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        asyncio.run(go())
        bridge._call.reset()
        bridge._stop_typing()


# ACK_MODE: what the bridge says the moment your words are typed in.

def test_ack_off_says_nothing_but_starts_typing(monkeypatch):
    """The default. On a real call a canned "Yup." right after a question sounded
    like the answer to the question. Silence plus typing says "working" instead."""
    h = Harness(monkeypatch, ack_mode="off")
    h.run("run the tests")
    assert h.injected[0][0] == "run the tests"
    assert h.spoken == []
    assert h.typing_started == 1


def test_ack_off_ignores_readback_setting(monkeypatch):
    """ACK_MODE wins. READBACK only picks the flavour when ACK_MODE=readback."""
    h = Harness(monkeypatch, mode="transcript", ack_mode="off")
    h.run("run the tests")
    assert h.spoken == []


def test_ack_short_speaks_canned_phrase(monkeypatch):
    h = Harness(monkeypatch, ack_mode="short")
    h.run("run the tests")
    assert h.injected[0][0] == "run the tests"
    assert len(h.spoken) == 1 and h.spoken[0] in bridge._ACK_PHRASES
    assert not any(s.startswith("I heard") for s in h.spoken)
    assert h.typing_started == 1


def test_ack_readback_speaks_the_read_back(monkeypatch):
    h = Harness(monkeypatch, mode="transcript", ack_mode="readback")
    h.run("run the tests")
    assert h.spoken == ["I heard: run the tests. Say stop to cancel."]


def test_ack_readback_with_readback_off_says_nothing(monkeypatch):
    """Documented corner: nothing to read back, so it behaves like ACK_MODE=off."""
    h = Harness(monkeypatch, mode="off", ack_mode="readback")
    h.run("run the tests")
    assert h.spoken == []
    assert h.typing_started == 1


def test_transcript_speaks_what_was_heard_and_hint_once(monkeypatch):
    h = Harness(monkeypatch, mode="transcript")
    h.run("um run the tests", "now push it")
    assert [t for t, _ in h.injected] == ["run the tests", "now push it"]
    assert h.spoken[0] == "I heard: run the tests. Say stop to cancel."
    assert h.spoken[1] == "I heard: now push it"


def test_transcript_truncates_long_text(monkeypatch):
    h = Harness(monkeypatch, mode="transcript")
    long = " ".join(f"word{i}" for i in range(40))
    h.run(long)
    assert h.injected[0][0] == long  # full text still injected
    assert "and more" in h.spoken[0]
    assert "word25" not in h.spoken[0]


def test_haiku_speaks_restatement(monkeypatch):
    h = Harness(monkeypatch, mode="haiku", haiku=lambda text, key, t: "Run the unit tests")
    h.run("run the tests")
    assert h.spoken == ["I heard: Run the unit tests. Say stop to cancel."]


def test_haiku_no_key_falls_back(monkeypatch, caplog):
    # Real haiku_restate: with no key it fails before any network call.
    h = Harness(monkeypatch, mode="haiku", key="")
    h.run("run the tests")
    assert h.spoken == ["I heard: run the tests. Say stop to cancel."]
    assert "ANTHROPIC_API_KEY" in caplog.text


def test_haiku_timeout_falls_back(monkeypatch, caplog):
    def slow(text, key, t):
        time.sleep(1.0)
        return "too late"
    h = Harness(monkeypatch, mode="haiku", haiku=slow, timeout_ms=200)
    h.run("run the tests")
    assert h.spoken == ["I heard: run the tests. Say stop to cancel."]
    assert "timed out" in caplog.text


def test_haiku_http_error_falls_back(monkeypatch, caplog):
    def http_error(text, key, t):
        raise readback.HaikuError("HTTP 500")
    h = Harness(monkeypatch, mode="haiku", haiku=http_error)
    h.run("run the tests")
    assert h.spoken == ["I heard: run the tests. Say stop to cancel."]
    assert "HTTP 500" in caplog.text


def test_injection_not_blocked_by_slow_haiku(monkeypatch):
    haiku_done = threading.Event()

    def slow(text, key, t):
        time.sleep(1.5)
        haiku_done.set()
        return "Run the tests"

    h = Harness(monkeypatch, mode="haiku", haiku=slow, timeout_ms=5000)
    inject_saw_haiku_done = []
    orig = bridge.inject_prompt

    def inject(text):
        inject_saw_haiku_done.append(haiku_done.is_set())
        return orig(text)
    monkeypatch.setattr(bridge, "inject_prompt", inject)

    h.run("run the tests")
    text, at = h.injected[0]
    assert text == "run the tests"
    assert at < 0.5, f"injection took {at:.2f}s"
    assert inject_saw_haiku_done == [False]
    assert h.spoken == ["I heard: Run the tests. Say stop to cancel."]


@pytest.mark.parametrize("word", ["stop", "Stop.", "cancel", "hold on", "Never mind."])
def test_stop_words_send_escape_not_injected(monkeypatch, word):
    h = Harness(monkeypatch, mode="transcript")
    h.run(word)
    assert h.injected == []
    assert h.escapes == 1
    assert h.spoken == ["Stopped."]
    assert bridge._turn_active is False


@pytest.mark.parametrize("text", ["stop the server", "don't stop now"])
def test_sentences_containing_stop_are_injected(monkeypatch, text):
    h = Harness(monkeypatch, mode="transcript")
    h.run(text)
    assert [t for t, _ in h.injected] == [text]
    assert h.escapes == 0


def test_custom_stop_words_override(monkeypatch):
    h = Harness(monkeypatch, mode="transcript")
    monkeypatch.setattr(bridge, "SETTINGS", replace(bridge.SETTINGS, stop_words=("abort",)))
    h.run("abort", "stop")
    assert h.escapes == 1
    assert [t for t, _ in h.injected] == ["stop"]
