"""PowerShell files must be pure ASCII.

claude-voice.ps1 shipped with three em dashes and no byte-order mark. Windows
PowerShell 5.1, which is what `powershell.exe` still is on a default Windows
install, reads a UTF-8 file with no BOM as the ANSI code page. The em dash
(U+2014, bytes E2 80 94) then decodes to three characters, the last of which is
a right curly quote, and that ends a string early:

    The string is missing the terminator: ".

pwsh 7 defaults to UTF-8, so the file parsed fine for anyone who had it and was
broken for everyone who did not. The README tells people to run this file, so
the first thing a new user did was hit a parse error.

Keeping these files ASCII removes the guess entirely.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PS1 = sorted(ROOT.glob("*.ps1"))


def test_there_are_ps1_files_to_check():
    assert PS1, "no .ps1 files found, so this test is not checking anything"


@pytest.mark.parametrize("path", PS1, ids=lambda p: p.name)
def test_ps1_is_ascii(path):
    raw = path.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    if not bad:
        return
    line = raw[: bad[0][0]].count(b"\n") + 1
    snippet = raw[max(0, bad[0][0] - 40) : bad[0][0] + 20]
    pytest.fail(
        f"{path.name} has {len(bad)} non-ASCII byte(s); first at line {line}: {snippet!r}. "
        "Windows PowerShell 5.1 reads this file as ANSI and can fail to parse it. "
        "Use a plain dash instead of an em dash, and ASCII instead of smart quotes."
    )
