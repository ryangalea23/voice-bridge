import io
import json
import urllib.error

import pytest

import readback
from voice_text import is_stop_command
from voice_settings import DEFAULT_STOP_WORDS


def test_short_text_not_truncated():
    assert readback.transcript_readback("delete the temp folder") == "I heard: delete the temp folder"


def test_long_text_truncated_to_20_words():
    text = " ".join(f"w{i}" for i in range(35))
    out = readback.transcript_readback(text)
    assert out == "I heard: " + " ".join(f"w{i}" for i in range(20)) + " and more"


def test_exactly_20_words_not_truncated():
    text = " ".join(f"w{i}" for i in range(20))
    assert not readback.transcript_readback(text).endswith("and more")


@pytest.mark.parametrize("text", ["stop", "Stop.", "cancel", "CANCEL!", "hold on", "Never mind.", " wait? "])
def test_stop_words_match(text):
    assert is_stop_command(text, DEFAULT_STOP_WORDS)


@pytest.mark.parametrize("text", ["stop the server", "don't stop now", "cancel the deploy", "wait for the build"])
def test_sentences_with_stop_words_do_not_match(text):
    assert not is_stop_command(text, DEFAULT_STOP_WORDS)


def test_haiku_no_key_raises():
    with pytest.raises(readback.HaikuError, match="ANTHROPIC_API_KEY"):
        readback.haiku_restate("hi", "", 1.0)


def test_haiku_http_error_raises(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 529, "overloaded", {}, None)
    monkeypatch.setattr(readback.urllib.request, "urlopen", boom)
    with pytest.raises(readback.HaikuError, match="HTTP 529"):
        readback.haiku_restate("hi", "key", 1.0)


def test_haiku_timeout_raises(monkeypatch):
    def slow(req, timeout):
        raise TimeoutError("timed out")
    monkeypatch.setattr(readback.urllib.request, "urlopen", slow)
    with pytest.raises(readback.HaikuError, match="request failed"):
        readback.haiku_restate("hi", "key", 1.0)


def test_haiku_success_parses_and_sends_headers(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake(req, timeout):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data)
        seen["timeout"] = timeout
        return Resp(json.dumps({"content": [{"type": "text", "text": " Restart the  staging server "}]}).encode())

    monkeypatch.setattr(readback.urllib.request, "urlopen", fake)
    out = readback.haiku_restate("um restart staging", "key", 2.5)
    assert out == "Restart the staging server"
    assert seen["headers"]["user-agent"].startswith("voice-bridge")
    assert seen["headers"]["x-api-key"] == "key"
    assert seen["body"]["model"] == "claude-haiku-4-5-20251001"
    assert seen["body"]["max_tokens"] <= 100
    assert seen["timeout"] == 2.5


def test_haiku_empty_output_raises(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(readback.urllib.request, "urlopen",
                        lambda req, timeout: Resp(b'{"content": []}'))
    with pytest.raises(readback.HaikuError, match="empty"):
        readback.haiku_restate("hi", "key", 1.0)
