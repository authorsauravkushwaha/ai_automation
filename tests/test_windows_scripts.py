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

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SUFFIXES = {".ps1", ".cmd", ".bat", ".psm1"}
UTF8_BOM = b"\xef\xbb\xbf"
# Characters the PowerShell tokenizer accepts as string delimiters.
POWERSHELL_QUOTE_LOOKALIKES = "\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f"


def script_files(root: Path = ROOT) -> list[Path]:
    # Setup creates .venv before running the suite. Its generated and installed
    # scripts are not part of this project and can be numerous or non-ASCII.
    excluded_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules"}
    files = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in excluded_dirs]
        for name in names:
            path = Path(directory) / name
            if path.is_file() and path.suffix.lower() in SCRIPT_SUFFIXES:
                files.append(path)
    return sorted(files)


class WindowsScriptEncodingTests(unittest.TestCase):
    def test_bootstrap_scripts_exist(self) -> None:
        names = {path.name for path in script_files()}
        self.assertIn("setup.ps1", names)
        self.assertIn("setup.cmd", names)
        self.assertIn("hinata.cmd", names)

    def test_script_discovery_excludes_virtual_environments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "setup.ps1").write_text("# project", encoding="ascii")
            external = root / ".venv" / "Scripts"
            external.mkdir(parents=True)
            (external / "external.ps1").write_bytes(b"\xff")
            self.assertEqual(script_files(root), [root / "setup.ps1"])

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

    @unittest.skipUnless(os.name == "nt", "requires Windows PowerShell and cmd.exe")
    def test_setup_cmd_parses_without_running_setup(self) -> None:
        # Exercise the same powershell.exe -File path used by users, including
        # argument forwarding. -CheckSyntax must take precedence over -Run.
        result = subprocess.run(
            f'call "{ROOT / "setup.cmd"}" -CheckSyntax -Run',
            cwd=ROOT,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Windows setup script syntax OK.", result.stdout)
        self.assertNotIn("[1/7]", result.stdout)

    def test_setup_script_connects_ollama_automatically(self) -> None:
        # A fresh Ollama install leaves the service stopped; setup must find the
        # executable (even off PATH) and start the loopback service itself.
        text = (ROOT / "setup.ps1").read_text(encoding="utf-8")
        for required in (
            "function Find-Ollama",
            "function Start-OllamaService",
            "Programs\\Ollama\\ollama.exe",
            '$env:OLLAMA_HOST = "127.0.0.1:11434"',
            "-ArgumentList @(\"serve\")",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        self.assertNotIn("Open a new terminal and rerun setup.", text)

    def test_start_bat_repairs_components_before_opening_the_gui(self) -> None:
        text = (ROOT / "start.bat").read_bytes().decode("ascii")
        self.assertIn("Reconnect", text)
        start = text.index(":START_APP")
        repair = text.index("call :ensure_components", start)
        launch = text.index("-m hinata gui", start)
        self.assertLess(repair, launch)
        self.assertIn("scripts\\ensure_components.py", text)

    def test_start_menu_guides_and_requires_setup_consent(self) -> None:
        text = (ROOT / "start.bat").read_bytes().decode("ascii")
        menu = text[text.index(":MENU"):text.index(":CHECK_LAUNCH")]
        for option in ("goto CONNECTED_SETUP", "goto DIAGNOSTICS", "goto GUIDE", "goto CHECK_LAUNCH"):
            self.assertIn(option, menu)
        setup = text[text.index(":CONNECTED_SETUP"):text.index(":START_APP")]
        self.assertLess(setup.index('if /i not "%ANS%"=="y" goto MENU'), setup.index('call :ensure_python'))
        self.assertLess(setup.index('call :ensure_python'), setup.index('setup.cmd" -Run'))
        guide = text[text.index(":GUIDE"):text.index("\n:ensure_python")]
        for detail in ("ollama pull nomic-embed-text", "ollama.auto_pull = false",
                       "does NOT disable networking", "OFFLINE_ISOLATION.md", "microphone and speaker"):
            self.assertIn(detail, guide)
        self.assertNotIn("setup.cmd", guide)

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe and choice.exe")
    def test_start_menu_exit_on_windows(self) -> None:
        result = subprocess.run(["cmd.exe", "/c", str(ROOT / "start.bat")], input="5", text=True,
                                capture_output=True, timeout=15, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Offline readiness and acceptance guide", result.stdout)
        self.assertNotIn("Starting the HINATA desktop", result.stdout)

    @unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe and choice.exe")
    def test_start_menu_guide_and_declined_setup_on_windows(self) -> None:
        # Isolate the launcher from an installed environment; never install or
        # download anything during these interaction tests.
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "start.bat"
            launcher.write_bytes((ROOT / "start.bat").read_bytes())
            for input_text, expected in (
                ("4m5", "Offline readiness checklist"),
                ("2n\n5", "Start connected installation and downloads now?"),
                ("1\n5", "HINATA is not installed yet"),
                ("3\n5", "HINATA is not installed yet"),
            ):
                with self.subTest(input_text=input_text):
                    result = subprocess.run(["cmd.exe", "/c", str(launcher)], cwd=tmp,
                                            input=input_text, text=True, capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(expected, result.stdout)
                    self.assertNotIn("Starting the HINATA desktop", result.stdout)
                    self.assertFalse((Path(tmp) / ".venv").exists())

    def test_setup_ps1_has_no_powershell_quote_lookalikes(self) -> None:
        text = (ROOT / "setup.ps1").read_bytes().decode("utf-8", "replace")
        found = sorted(
            {character for character in text if character in POWERSHELL_QUOTE_LOOKALIKES}
        )
        self.assertFalse(found, f"setup.ps1 must not contain curly quotes: {found}")


if __name__ == "__main__":
    unittest.main()
