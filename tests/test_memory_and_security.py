import json
import tempfile
import unittest
from pathlib import Path

from hinata.core.memory import Memory, MemoryPolicyError
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

    def test_configured_conversation_retention_prunes_only_messages(self) -> None:
        self.memory.set_preference("editor", "vim")
        memory = Memory(self.database, conversation_limit=2)
        for text in ("first", "second", "third"):
            memory.add_message("user", text)
        self.assertEqual([row["content"] for row in memory.recent_messages()], ["second", "third"])
        self.assertEqual(self.memory.preferences()[0]["value"], "vim")
        with self.assertRaises(ValueError):
            Memory(self.database, conversation_limit=0)

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


class EpisodeEvidenceTests(unittest.TestCase):
    """``evidence`` must reach storage: it used to be accepted and dropped."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "hinata.db")
        self.memory = Memory(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_evidence_is_persisted_with_the_episode(self) -> None:
        memory_id = self.memory.record_episode(
            task_id="t1",
            objective="backup created",
            outcome="COMPLETED",
            summary="archived Documents",
            evidence="sha256:abc123 wrote 42 files",
        )
        row = self.database.fetch_one(
            "SELECT content FROM memory_records WHERE memory_id = ?", (memory_id,)
        )
        self.assertIn("sha256:abc123", row["content"])
        self.assertIn("archived Documents", row["content"])

    def test_episode_without_evidence_is_unchanged(self) -> None:
        memory_id = self.memory.record_episode(
            task_id="t2", objective="noop", outcome="COMPLETED", summary="nothing"
        )
        row = self.database.fetch_one(
            "SELECT content FROM memory_records WHERE memory_id = ?", (memory_id,)
        )
        self.assertNotIn("Evidence:", row["content"])

    def test_secret_material_in_evidence_is_refused(self) -> None:
        with self.assertRaises(MemoryPolicyError):
            self.memory.record_episode(
                task_id="t3",
                objective="api call",
                outcome="COMPLETED",
                summary="called api",
                evidence="Authorization: Bearer tok_secret_123456",
            )


if __name__ == "__main__":
    unittest.main()
