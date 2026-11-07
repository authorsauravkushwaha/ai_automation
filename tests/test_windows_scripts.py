"""Guard the Windows bootstrap scripts against encoding-driven parse failures.

Windows PowerShell 5.1 (the version behind ``powershell.exe``, which
``setup.cmd`` uses) reads a BOM-less script file with the active ANSI code page
instead of UTF-8. A UTF-8 em dash such as ``E2 80 94`` therefore decodes to
``-<0x80>-<U+201D>`` under Windows-1252, and U+201D is a character the
PowerShell parser accepts as a closing double quote. That stray quote
desynchronises string parsing for the rest of the file, which is why a harmless
``Write-Host "setup - local"`` line produced errors such as::

    At setup.ps1:77 char:37
    + $VenvPython = Join-Path $Root ".venv\\Scripts\\python.exe"
    Unexpected token '\\Scripts\\python.exe"' in expression or statement.

Keeping the shipped scripts ASCII-only (or explicitly BOM-prefixed UTF-8) makes
the decoding identical under every PowerShell host.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SUFFIXES = {".ps1", ".cmd", ".bat", ".psm1"}
UTF8_BOM = b"\xef\xbb\xbf"
# Characters the PowerShell tokenizer accepts as string delimiters.
POWERSHELL_QUOTE_LOOKALIKES = "\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f"


def script_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in SCRIPT_SUFFIXES and ".git" not in path.parts
    )


class WindowsScriptEncodingTests(unittest.TestCase):
    def test_bootstrap_scripts_exist(self) -> None:
        names = {path.name for path in script_files()}
        self.assertIn("setup.ps1", names)
        self.assertIn("setup.cmd", names)
        self.assertIn("hinata.cmd", names)

    def test_bootstrap_scripts_are_ascii_or_bom_prefixed(self) -> None:
        for path in script_files():
            raw = path.read_bytes()
            if raw.startswith(UTF8_BOM):
                continue
            decoded = raw.decode("latin-1")
            offenders = [
                (number, line)
                for number, line in enumerate(decoded.splitlines(), 1)
                if any(ord(character) > 127 for character in line)
            ]
            if offenders:
                self.fail(
                    f"{path.relative_to(ROOT)} must stay ASCII-only or start with a UTF-8 BOM; "
                    f"Windows PowerShell 5.1 mis-decodes non-ASCII bytes. "
                    f"Offending lines: {offenders[:3]}"
                )

    def test_setup_ps1_has_no_powershell_quote_lookalikes(self) -> None:
        text = (ROOT / "setup.ps1").read_bytes().decode("utf-8", "replace")
        found = sorted(
            {character for character in text if character in POWERSHELL_QUOTE_LOOKALIKES}
        )
        self.assertFalse(found, f"setup.ps1 must not contain curly quotes: {found}")


if __name__ == "__main__":
    unittest.main()
