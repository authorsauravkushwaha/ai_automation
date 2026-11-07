import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hinata.config import ConfigError, HinataConfig, default_data_dir, load_config


class ConfigTests(unittest.TestCase):
    def write_config(self, content: str) -> str:
        temporary = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
        with temporary:
            temporary.write(content)
        self.addCleanup(Path(temporary.name).unlink)
        return temporary.name

    def test_remote_llm_hosts_are_rejected(self) -> None:
        path = self.write_config('[ollama]\nhost = "https://api.openai.com"\n')
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_unknown_keys_are_rejected(self) -> None:
        path = self.write_config("[ollama]\nremote_api_key = 'secret'\n")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_custom_allowlisted_application(self) -> None:
        path = self.write_config(
            '[ollama]\nenabled = false\n[applications.notepad]\ncommand = ["notepad.exe"]\nprocess_names = ["notepad.exe"]\n'
        )
        config = load_config(path)
        self.assertFalse(config.ollama.enabled)
        self.assertEqual(config.applications["notepad"].command, ["notepad.exe"])

    def test_existing_friday_user_data_location_is_reused_without_moving_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "friday"
            legacy.mkdir()
            with patch.dict(os.environ, {"XDG_DATA_HOME": directory}, clear=True):
                self.assertEqual(default_data_dir(), legacy)
            self.assertTrue(legacy.is_dir())

    def test_existing_legacy_database_and_log_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "friday.db").touch()
            (data / "logs").mkdir()
            (data / "logs" / "friday.log").touch()
            config = HinataConfig(data_dir=data)
            self.assertEqual(config.database_path.name, "friday.db")
            self.assertEqual(config.log_path.name, "friday.log")


if __name__ == "__main__":
    unittest.main()
