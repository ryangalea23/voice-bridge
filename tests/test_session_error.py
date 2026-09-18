"""When the launcher could not find a window that accepts typed text, the caller
should hear why, not the generic "session not active"."""
import bridge
import inject


def test_generic_phrase_when_there_is_no_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(inject, "HWND_ERROR_FILE", str(tmp_path / "missing.error"))
    assert bridge._no_session_phrase("Nothing to stop.") == "Session not active. Nothing to stop."


def test_launcher_reason_is_spoken_instead(tmp_path, monkeypatch):
    reason = "This terminal cannot receive typed text. Start the voice launcher in a plain console window, then call back."
    path = tmp_path / "voice-session.hwnd.error"
    path.write_text(reason, encoding="utf-8")
    monkeypatch.setattr(inject, "HWND_ERROR_FILE", str(path))
    assert bridge._no_session_phrase("Nothing to stop.") == reason
