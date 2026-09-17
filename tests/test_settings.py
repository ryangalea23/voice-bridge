import logging

from voice_settings import DEFAULT_STOP_WORDS, load_settings


def test_defaults():
    s = load_settings({})
    assert s.readback == "transcript"
    assert s.haiku_timeout_ms == 2500
    assert s.stop_words == DEFAULT_STOP_WORDS
    assert s.deepgram_keyterms == ()
    assert s.anthropic_api_key == ""


def test_valid_values():
    s = load_settings({
        "READBACK": "Haiku",
        "READBACK_HAIKU_TIMEOUT_MS": "1800",
        "VOICE_STOP_WORDS": "Halt, abort ,  hang   on",
        "DEEPGRAM_KEYTERMS": "Twilio, Deepgram,,",
        "ANTHROPIC_API_KEY": " k ",
    })
    assert s.readback == "haiku"
    assert s.haiku_timeout_ms == 1800
    assert s.stop_words == ("halt", "abort", "hang on")
    assert s.deepgram_keyterms == ("Twilio", "Deepgram")
    assert s.anthropic_api_key == "k"


def test_invalid_readback_falls_back_and_logs(caplog):
    with caplog.at_level(logging.WARNING):
        s = load_settings({"READBACK": "loud"})
    assert s.readback == "transcript"
    assert "READBACK='loud'" in caplog.text


def test_invalid_timeout_falls_back_and_logs(caplog):
    for bad in ("soon", "-5", "0"):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            s = load_settings({"READBACK_HAIKU_TIMEOUT_MS": bad})
        assert s.haiku_timeout_ms == 2500
        assert "READBACK_HAIKU_TIMEOUT_MS" in caplog.text


def test_empty_stop_words_falls_back_and_logs(caplog):
    with caplog.at_level(logging.WARNING):
        s = load_settings({"VOICE_STOP_WORDS": " , ,"})
    assert s.stop_words == DEFAULT_STOP_WORDS
    assert "VOICE_STOP_WORDS" in caplog.text
