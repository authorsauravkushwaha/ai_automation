import json
import tempfile
import unittest
from pathlib import Path

from hinata.core.memory import Memory
from hinata.database.database import Database
from hinata.security.authentication import Authentication
from hinata.security.local_protection import (
    protect_bytes,
    read_private,
    unprotect_bytes,
    write_private,
)


class MemoryAndSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "hinata.db")
        self.memory = Memory(self.database)
        self.auth = Authentication(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pin_is_salted_and_verified(self) -> None:
        self.auth.set_pin("2468")
        self.assertTrue(self.auth.verify_pin("2468"))
        self.assertFalse(self.auth.verify_pin("0000"))
        stored = self.database.get_setting(self.auth.SETTING_KEY)
        self.assertNotIn("2468", stored)
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))

    def test_pin_rules(self) -> None:
        for pin in ("123", "abcdefgh", "１２３４", "1" * 13):
            with self.assertRaises(ValueError):
                self.auth.set_pin(pin)

    def test_memory_search_escapes_wildcards(self) -> None:
        self.memory.add_message("user", "literal 100%_value")
        self.memory.add_message("user", "100XXvalue")
        rows = self.memory.search("100%_")
        self.assertEqual(len(rows), 1)
        self.assertIn("literal", rows[0]["text"])

    def test_export_excludes_settings_and_pin(self) -> None:
        self.auth.set_pin("1234")
        self.memory.set_preference("editor", "VS Code")
        destination = self.database.export_json(self.root / "export.json")
        exported = json.loads(destination.read_text(encoding="utf-8"))
        self.assertNotIn("settings", exported)
        self.assertNotIn(self.auth.SETTING_KEY, destination.read_text(encoding="utf-8"))
        self.assertEqual(exported["preferences"][0]["value"], "VS Code")

    def test_private_protection_round_trip(self) -> None:
        payload = b"voice embedding bytes"
        self.assertEqual(unprotect_bytes(protect_bytes(payload)), payload)
        path = self.root / "profiles" / "owner.voice"
        write_private(path, payload)
        self.assertEqual(read_private(path), payload)


if __name__ == "__main__":
    unittest.main()
