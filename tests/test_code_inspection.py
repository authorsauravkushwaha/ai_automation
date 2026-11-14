import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_assistant


class CodeInspectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.tmp.name)
        self.root = Path(self.tmp.name) / "Documents"

    def tearDown(self):
        self.assistant.close()
        self.tmp.cleanup()

    def test_static_code_analysis_does_not_execute_source(self):
        code = self.root / "example.py"
        marker = self.root / "SHOULD_NOT_EXIST"
        code.write_text(f'''import subprocess
from pathlib import Path
Path({str(marker)!r}).write_text("executed")
class Example:
    async def run(self):
        eval("1 + 1")
        subprocess.run("echo x", shell=True)
''', encoding="utf-8")
        result = self.assistant.process_text("inspect code Documents/example.py")
        self.assertTrue(result.success, result.text)
        self.assertTrue(result.data["valid_syntax"])
        self.assertEqual(result.data["classes"][0]["name"], "Example")
        self.assertEqual(result.data["functions"][0]["name"], "run")
        self.assertEqual(len(result.data["warnings"]), 2)
        self.assertFalse(marker.exists())
        self.assertEqual(self.assistant.orchestrator.workers.recent()[0]["worker"], "coding")

    def test_invalid_python_is_reported_not_executed(self):
        (self.root / "bad.py").write_text("def missing(:\n", encoding="utf-8")
        result = self.assistant.process_text("inspect python Documents/bad.py")
        self.assertTrue(result.success)
        self.assertFalse(result.data["valid_syntax"])
        self.assertEqual(result.data["syntax_error"]["line"], 1)

    def test_outside_root_wrong_extension_and_large_file_rejected(self):
        outside = Path(self.tmp.name) / "outside.py"
        outside.write_text("x = 1", encoding="utf-8")
        self.assertFalse(self.assistant.process_text(f"inspect code {outside}").success)
        (self.root / "notes.txt").write_text("x = 1", encoding="utf-8")
        self.assertFalse(self.assistant.process_text("inspect code Documents/notes.txt").success)
        (self.root / "huge.py").write_text("a" * 20001, encoding="utf-8")
        self.assertFalse(self.assistant.process_text("inspect code Documents/huge.py").success)

    def test_symlink_escape_is_rejected(self):
        outside = Path(self.tmp.name) / "outside.py"
        outside.write_text("x = 1", encoding="utf-8")
        try:
            (self.root / "link.py").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertFalse(self.assistant.process_text("inspect code Documents/link.py").success)

    def test_result_list_is_bounded(self):
        (self.root / "many.py").write_text("\n".join(f"def f{i}(): pass" for i in range(110)), encoding="utf-8")
        result = self.assistant.process_text("inspect python Documents/many.py")
        self.assertEqual(len(result.data["functions"]), 100)
        self.assertTrue(result.data["truncated"])


if __name__ == "__main__":
    unittest.main()
