"""Offline speech diagnostics, saved mute preference, and setup wiring."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hinata.config import InterfaceConfig, SpeechConfig
from hinata.main import main
from hinata.speech.replies import speaking_enabled
from hinata.speech.text_to_speech import output_readiness

ROOT = Path(__file__).resolve().parent.parent


class VoiceOutputReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "data"
        self.config = Path(self.temporary.name) / "legacy.toml"
        self.config.write_text("[interface]\nspeak_responses = false\n")
        self.env_patch = patch.dict(os.environ, {"HINATA_DATA_DIR": str(self.data_dir)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def call_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--config", str(self.config), *args])
        return code, output.getvalue()

    def test_legacy_mute_can_be_enabled_without_editing_config(self) -> None:
        code, output = self.call_cli("voice-output", "status")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["replies_enabled"])
        self.assertEqual(self.call_cli("voice-output", "enable")[0], 0)
        self.assertFalse(self.config.read_text().endswith("true\n"))
        self.assertTrue(json.loads(self.call_cli("voice-output", "status")[1])["replies_enabled"])
        with patch("hinata.main.emit_with_approvals", return_value=0) as emit:
            self.assertEqual(self.call_cli("--no-llm", "ask", "hello")[0], 0)
            self.assertTrue(emit.call_args.kwargs["speak"])
        self.assertEqual(self.call_cli("voice-output", "disable")[0], 0)
        self.assertFalse(json.loads(self.call_cli("voice-output", "status")[1])["replies_enabled"])
        with patch("hinata.main.emit_with_approvals", return_value=0) as emit:
            self.assertEqual(self.call_cli("--no-llm", "ask", "hello")[0], 0)
            self.assertFalse(emit.call_args.kwargs["speak"])

    def test_readiness_never_claims_actual_device_works(self) -> None:
        with patch("hinata.speech.text_to_speech.importlib.util.find_spec", return_value=None):
            result = output_readiness(SpeechConfig())
        self.assertFalse(result["backend_ready"])
        self.assertIn("pip install", result["guidance"])
        with patch("hinata.speech.text_to_speech.importlib.util.find_spec", return_value=object()):
            result = output_readiness(SpeechConfig())
        self.assertTrue(result["backend_ready"])
        self.assertIn("test", result["guidance"])
        self.assertFalse(output_readiness(SpeechConfig(tts_backend="piper"))["backend_ready"])

    def test_voice_test_checks_backend_and_reports_failure(self) -> None:
        with patch("hinata.speech.text_to_speech.output_readiness", return_value={
            "backend": "pyttsx3", "backend_ready": False, "guidance": "Install pyttsx3"
        }), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.call_cli("voice-output", "test")[0], 2)
        with patch("hinata.speech.text_to_speech.output_readiness", return_value={
            "backend": "pyttsx3", "backend_ready": True, "guidance": "test audio"
        }), patch("hinata.speech.text_to_speech.TextToSpeech.speak") as speak, patch(
            "hinata.speech.text_to_speech.TextToSpeech.wait"
        ) as wait:
            code, output = self.call_cli("voice-output", "test")
        self.assertEqual(code, 0)
        self.assertIn("Voice test completed", output)
        speak.assert_called_once()
        wait.assert_called_once_with(timeout=20.0)

        def fail_device(tts: object, text: str) -> None:
            tts.last_error = "no output device"

        with patch("hinata.speech.text_to_speech.output_readiness", return_value={
            "backend": "pyttsx3", "backend_ready": True, "guidance": "test audio"
        }), patch("hinata.speech.text_to_speech.TextToSpeech.speak", autospec=True,
                  side_effect=fail_device), patch("hinata.speech.text_to_speech.TextToSpeech.wait"), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(self.call_cli("voice-output", "test")[0], 2)
        self.assertIn("no output device", errors.getvalue())

    def test_setup_installs_tts_in_lite_and_offers_private_legacy_upgrade(self) -> None:
        script = (ROOT / "setup.ps1").read_bytes().decode("ascii")
        self.assertIn('$Extras = @("tts")', script)
        self.assertIn('Confirm-OptionalDownload "Enable spoken replies', script)
        self.assertIn('voice-output", "enable"', script)
        self.assertLess(script.index('Confirm-OptionalDownload "Play a short offline voice test'),
                        script.index('hinata voice-output test'))

    def test_saved_choice_precedence(self) -> None:
        class Store:
            value: str | None = None

            def get_setting(self, key: str) -> str | None:
                self.assert_key(key)
                return self.value

            @staticmethod
            def assert_key(key: str) -> None:
                if key != "ui.speak_responses":
                    raise AssertionError(key)

        store = Store()
        self.assertFalse(speaking_enabled(InterfaceConfig(speak_responses=False), store))
        store.value = "true"
        self.assertTrue(speaking_enabled(InterfaceConfig(speak_responses=False), store))
        store.value = "false"
        self.assertFalse(speaking_enabled(InterfaceConfig(speak_responses=True), store))


if __name__ == "__main__":
    unittest.main()
