import tempfile
import unittest
from pathlib import Path

from friday.automation.app_control import AutomationError
from friday.automation.file_manager import FileManager
from friday.config import SecurityConfig


class FileManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.allowed = self.root / "Documents"
        self.allowed.mkdir()
        self.outside = self.root / "private"
        self.outside.mkdir()
        self.manager = FileManager(SecurityConfig(allowed_roots=[self.allowed]))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_read_and_search(self) -> None:
        created = self.manager.create_folder("Projects")
        self.assertTrue(Path(created["path"]).is_dir())
        note = self.allowed / "Projects" / "report notes.txt"
        note.write_text("offline result", encoding="utf-8")
        result = self.manager.read_text("Projects/report notes.txt")
        self.assertEqual(result["content"], "offline result")
        found = self.manager.search("report", max_results=5)
        self.assertEqual(found["matches"], [str(note)])

    def test_path_traversal_is_denied(self) -> None:
        secret = self.outside / "secret.txt"
        secret.write_text("do not read", encoding="utf-8")
        for path in (str(secret), "../private/secret.txt"):
            with self.assertRaises(AutomationError):
                self.manager.read_text(path)
        with self.assertRaises(AutomationError):
            self.manager.create_folder("../escape")

    def test_extension_and_size_limits(self) -> None:
        binary = self.allowed / "image.png"
        binary.write_bytes(b"not text")
        with self.assertRaises(AutomationError):
            self.manager.read_text("image.png")
        executable = self.allowed / "bad.exe"
        executable.write_bytes(b"x")
        with self.assertRaises(AutomationError):
            self.manager.open_file("bad.exe")

    def test_exact_character_boundary_is_not_false_truncation(self) -> None:
        note = self.allowed / "exact.txt"
        note.write_text("12345", encoding="utf-8")
        result = self.manager.read_text("exact.txt", max_chars=5)
        self.assertFalse(result["truncated"])
        result = self.manager.read_text("exact.txt", max_chars=4)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["content"], "1234")

    def test_root_name_resolves(self) -> None:
        self.assertEqual(self.manager.resolve("Documents"), self.allowed.resolve())


if __name__ == "__main__":
    unittest.main()
