"""The console input buffer path: how it finds the session, how it hands text to
its child process, and how inject.py chooses between it and the old key path."""
import os
import subprocess
import sys

import pytest

import console_inject
import inject


# ------------------------------------------------------------- target discovery

@pytest.fixture
def no_target(tmp_path, monkeypatch):
    """Point both discovery sources at files that do not exist."""
    monkeypatch.setattr(console_inject, "PID_FILE", str(tmp_path / "missing.pid"))
    monkeypatch.setattr(console_inject, "HWND_FILE", str(tmp_path / "missing.hwnd"))
    return tmp_path


def test_pid_file_wins(no_target, monkeypatch):
    (no_target / "missing.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(console_inject, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(console_inject, "_pid_from_hwnd", lambda: 9999)
    assert console_inject.find_target() == (4242, "pid file")


def test_hwnd_is_the_fallback(no_target, monkeypatch):
    monkeypatch.setattr(console_inject, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(console_inject, "_pid_from_hwnd", lambda: 777)
    pid, how = console_inject.find_target()
    assert pid == 777
    assert "window handle" in how


def test_stale_pid_file_falls_through_and_says_so(no_target, monkeypatch):
    (no_target / "missing.pid").write_text("4242", encoding="utf-8")
    monkeypatch.setattr(console_inject, "_pid_alive", lambda pid: pid != 4242)
    monkeypatch.setattr(console_inject, "_pid_from_hwnd", lambda: 777)
    pid, how = console_inject.find_target()
    assert pid == 777
    assert "stale" in how


def test_neither_source_present(no_target):
    pid, how = console_inject.find_target()
    assert pid is None
    assert "no live session process" in how


def test_injection_reports_missing_target_without_spawning(no_target, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not spawn a child with no target")

    monkeypatch.setattr(subprocess, "run", boom)
    ok, reason = console_inject.inject_text("hello")
    assert ok is False
    assert "no live session process" in reason


# ------------------------------------------------------ handing text to the child

class _FakeCompleted:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = stderr


@pytest.fixture
def fixed_target(monkeypatch):
    monkeypatch.setattr(console_inject, "find_target", lambda: (1234, "pid file"))


@pytest.mark.parametrize("text", [
    'quotes "double" and \'single\'',
    "two\nlines",
    "unicode: naïve café — ✓ 日本語",
    "shell metacharacters & | > < $(whoami) `id`",
])
def test_text_goes_over_stdin_never_the_command_line(fixed_target, monkeypatch, text):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs["input"]
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, _ = console_inject.inject_text(text)
    assert ok is True
    assert seen["input"] == text.encode("utf-8")
    assert all(text not in part for part in seen["cmd"])


def test_child_gets_the_text_byte_for_byte():
    """A real child process, to prove the stdin encoding survives the trip."""
    text = "café — 日本語 \"quoted\" \nsecond line"
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        input=text.encode("utf-8"),
        capture_output=True,
    )
    assert proc.stdout.decode("utf-8") == text


def test_timeout_is_reported_not_raised(fixed_target, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, reason = console_inject.inject_text("hello", timeout=0.25)
    assert ok is False
    assert "timed out after 0.25s" in reason


def test_timeout_is_passed_to_the_child(fixed_target, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    console_inject.inject_text("hello")
    assert seen["timeout"] == console_inject.DEFAULT_TIMEOUT


def test_child_failure_reason_is_kept(fixed_target, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: _FakeCompleted(1, b"AttachConsole(1234) failed, error 87\n"),
    )
    ok, reason = console_inject.inject_text("hello")
    assert ok is False
    assert "error 87" in reason


def test_escape_sends_no_text(fixed_target, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs["input"]
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, _ = console_inject.send_escape()
    assert ok is True
    assert seen["input"] == b""
    assert "escape" in seen["cmd"]


# ----------------------------------------------------------------- key records

def test_escape_records_are_one_key_down_and_up():
    records = console_inject.build_records("escape", "")
    assert len(records) == 2
    assert [bool(r.Event.KeyEvent.bKeyDown) for r in records] == [True, False]
    assert records[0].Event.KeyEvent.wVirtualKeyCode == 0x1B
    assert records[0].Event.KeyEvent.uChar.UnicodeChar == "\x1b"


def test_text_records_end_with_a_carriage_return():
    records = console_inject.build_records("text", "hi")
    assert len(records) == 6  # h, i, CR, each with a down and an up
    assert [r.Event.KeyEvent.uChar.UnicodeChar for r in records[::2]] == ["h", "i", "\r"]
    assert records[-1].Event.KeyEvent.wVirtualKeyCode == 0x0D


def test_newlines_become_carriage_returns():
    records = console_inject.build_records("text", "a\nb")
    chars = [r.Event.KeyEvent.uChar.UnicodeChar for r in records[::2]]
    assert chars == ["a", "\r", "b", "\r"]


# --------------------------------------------------------------- method chooser

@pytest.fixture
def no_env_method(monkeypatch):
    monkeypatch.delenv("INJECT_METHOD", raising=False)


def test_default_method_is_auto(no_env_method):
    assert inject.inject_method() == "auto"


@pytest.mark.parametrize("raw,expected", [
    ("console", "console"), ("KEYS", "keys"), (" auto ", "auto"),
    ("nonsense", "auto"), ("", "auto"),
])
def test_method_is_read_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("INJECT_METHOD", raw)
    assert inject.inject_method() == expected


def _record_calls(monkeypatch, console_ok, keys_ok):
    calls = []

    def fake_console(text):
        calls.append("console")
        return (console_ok, "pid 1 via pid file" if console_ok else "AttachConsole failed")

    def fake_keys(text):
        calls.append("keys")
        return keys_ok

    monkeypatch.setattr(console_inject, "inject_text", fake_console)
    monkeypatch.setattr(inject, "_inject_prompt_keys", fake_keys)
    return calls


def test_auto_uses_console_when_it_works(monkeypatch, no_env_method):
    calls = _record_calls(monkeypatch, console_ok=True, keys_ok=True)
    assert inject.inject_prompt("hello") is True
    assert calls == ["console"]


def test_auto_falls_back_to_keys_and_logs_why(monkeypatch, no_env_method, caplog):
    calls = _record_calls(monkeypatch, console_ok=False, keys_ok=True)
    with caplog.at_level("INFO"):
        assert inject.inject_prompt("hello") is True
    assert calls == ["console", "keys"]
    assert "AttachConsole failed" in caplog.text
    assert "Falling back" in caplog.text


def test_console_only_never_touches_the_keys_path(monkeypatch):
    monkeypatch.setenv("INJECT_METHOD", "console")
    calls = _record_calls(monkeypatch, console_ok=False, keys_ok=True)
    assert inject.inject_prompt("hello") is False
    assert calls == ["console"]


def test_keys_only_never_touches_the_console_path(monkeypatch):
    monkeypatch.setenv("INJECT_METHOD", "keys")
    calls = _record_calls(monkeypatch, console_ok=True, keys_ok=True)
    assert inject.inject_prompt("hello") is True
    assert calls == ["keys"]


def test_returns_false_when_both_paths_fail(monkeypatch, no_env_method):
    calls = _record_calls(monkeypatch, console_ok=False, keys_ok=False)
    assert inject.inject_prompt("hello") is False
    assert calls == ["console", "keys"]


def test_console_path_raising_does_not_escape(monkeypatch, no_env_method):
    def boom(text):
        raise RuntimeError("ctypes exploded")

    monkeypatch.setattr(console_inject, "inject_text", boom)
    monkeypatch.setattr(inject, "_inject_prompt_keys", lambda text: False)
    assert inject.inject_prompt("hello") is False


def test_escape_chooser_falls_back_too(monkeypatch, no_env_method):
    calls = []
    monkeypatch.setattr(console_inject, "send_escape",
                        lambda: (calls.append("console"), (False, "no session"))[1])
    monkeypatch.setattr(inject, "_send_escape_keys",
                        lambda: (calls.append("keys"), True)[1])
    assert inject.send_escape() is True
    assert calls == ["console", "keys"]
