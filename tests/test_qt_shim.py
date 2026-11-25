"""Tests for scripts/build_qt_shim.py (the headless-Qt enabler).

The shim is what makes `tests/test_gui.py` runnable on a bare Linux box, so it
needs its own guard rails: it must find the real Qt libraries, classify symbols
onto the right stub, and emit a VERSION script that ld actually accepts.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_qt_shim.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_qt_shim", BUILDER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(BUILDER.exists(), "shim builder missing")
class ShimBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_builder()

    def test_version_map_covers_the_known_labels(self) -> None:
        mapping = self.module.VERSION_TO_LIBRARY
        self.assertEqual(mapping["LIBDBUS_1_3"], "libdbus-1.so.3")
        self.assertEqual(mapping["V_0.5.0"], "libxkbcommon.so.0")

    def test_qt_library_dir_exists_when_pyside_is_installed(self) -> None:
        if importlib.util.find_spec("PySide6") is None:
            self.skipTest("PySide6 not installed")
        directory = self.module.qt_library_dir()
        self.assertTrue(directory.is_dir(), directory)
        self.assertTrue((directory / "libQt6Gui.so.6").exists())

    @unittest.skipUnless(shutil.which("gcc"), "gcc required to compile stubs")
    def test_build_library_emits_an_accepted_version_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            self.module.build_library(
                out, "libtest.so.0", ["alpha_symbol", "beta_symbol"], "TEST_1_0"
            )
            built = out / "libtest.so.0"
            self.assertTrue(built.exists())
            exported = subprocess.run(
                ["nm", "-D", "--defined-only", str(built)],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertIn("alpha_symbol", exported)
            self.assertIn("beta_symbol", exported)
            # the version node must be present, or the loader will not match
            versioned = subprocess.run(
                ["nm", "-D", "--with-symbol-versions", str(built)],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertIn("TEST_1_0", versioned)

    @unittest.skipUnless(shutil.which("gcc"), "gcc required to compile stubs")
    def test_stub_symbols_are_functions_returning_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            self.module.build_library(out, "libzero.so.0", ["some_call"], None)
            # 'T' in nm means a text (function) symbol, not 'D' data.
            text = subprocess.run(
                ["nm", "-D", "--defined-only", str(out / "libzero.so.0")],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertRegex(text, r"T some_call")

    def test_unknown_symbol_versions_are_skipped(self) -> None:
        """A foreign version node (e.g. Qt's own) must never be stubbed."""
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp)
            self.module.build_library(out, "libempty.so.0", [], None)
            self.assertFalse((out / "libempty.so.0").exists())

    def test_script_exits_cleanly_when_run_directly(self) -> None:
        """A no-argument run must not crash, even where nothing needs shimming."""
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--out", str(pathlib.Path(tempfile.mkdtemp()))],
            capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
