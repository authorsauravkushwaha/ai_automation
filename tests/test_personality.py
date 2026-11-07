import tempfile
import unittest
from pathlib import Path

from hinata.config import ConfigError, load_config
from hinata.core.assistant import HinataAssistant
from hinata.core.personality import MODES, PersonalityManager
from hinata.database.database import Database
from tests.helpers import make_assistant


class PersonalityTests(unittest.TestCase):
    def test_all_five_modes_are_available_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "memory.db")
            manager = PersonalityManager(database)
            self.assertEqual(set(MODES), {"friend", "advisor", "technical", "strategist", "professional"})
            selected = manager.set_mode("technical")
            self.assertEqual(selected.key, "technical")
            self.assertEqual(PersonalityManager(database).mode.key, "technical")

    def test_text_mode_command_changes_active_mode_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            assistant = make_assistant(directory)
            try:
                response = assistant.process_text("switch to strategist mode")
                self.assertTrue(response.success)
                self.assertFalse(response.needs_confirmation)
                self.assertEqual(assistant.personality.mode.key, "strategist")
                self.assertEqual(assistant.database.get_setting("personality.mode"), "strategist")
            finally:
                assistant.close()

    def test_config_rejects_unknown_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[personality]\nmode = "unrestricted"\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_modes_do_not_change_permission_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            assistant = make_assistant(directory)
            try:
                assistant.personality.set_mode("advisor")
                sensitive = assistant.process_text("clear memory")
                self.assertTrue(sensitive.needs_confirmation)
            finally:
                assistant.close()


if __name__ == "__main__":
    unittest.main()
