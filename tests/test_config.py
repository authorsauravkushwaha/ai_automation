import tempfile
import unittest
from pathlib import Path

from friday.config import ConfigError, load_config


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


if __name__ == "__main__":
    unittest.main()
