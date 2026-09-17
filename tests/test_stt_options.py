from stt import build_live_options, keyterm_options


def test_nova2_uses_keywords():
    opts = build_live_options("mulaw", 8000, 1, ["Twilio", "Deepgram"])
    d = opts.to_dict()
    assert d["model"] == "nova-2"
    assert d["keywords"] == ["Twilio", "Deepgram"]
    assert "keyterm" not in d


def test_nova3_uses_keyterm():
    d = build_live_options("mulaw", 8000, 1, ["Twilio"], model="nova-3").to_dict()
    assert d["keyterm"] == ["Twilio"]
    assert "keywords" not in d


def test_empty_keyterms_omitted():
    for empty in (None, [], ()):
        d = build_live_options("mulaw", 8000, 1, empty).to_dict()
        assert "keywords" not in d and "keyterm" not in d
    assert keyterm_options("nova-3", []) == {}
