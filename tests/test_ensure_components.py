"""start.bat delegates every launch to scripts/ensure_components.py."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import ensure_components


class MissingComponentsTests(unittest.TestCase):
    def test_nothing_missing_when_all_modules_present(self) -> None:
        with patch.object(ensure_components, "module_installed", return_value=True):
            self.assertEqual(ensure_components.missing_components(), [])

    def test_missing_configured_whisper_backend_is_reported(self) -> None:
        def present(name: str) -> bool:
            return name != "faster_whisper"

        with patch.object(ensure_components, "module_installed", side_effect=present):
            missing = ensure_components.missing_components(_config())
        self.assertEqual(missing, ["faster_whisper (offline speech)"])

    def test_vosk_configuration_requires_vosk_instead_of_whisper(self) -> None:
        def present(name: str) -> bool:
            return name != "vosk"

        with patch.object(ensure_components, "module_installed", side_effect=present):
            missing = ensure_components.missing_components(_config("vosk", "", "model"))
        self.assertEqual(missing, ["vosk (offline speech)"])

    def test_core_piece_is_named_when_absent(self) -> None:
        def present(name: str) -> bool:
            return name != "PySide6" and name in {
                "PySide6",
                "sounddevice",
                "pyttsx3",
                "pycaw",
                "faster_whisper",
                "vosk",
            }

        with patch.object(ensure_components, "module_installed", side_effect=present):
            missing = ensure_components.missing_components()
        self.assertEqual(missing, ["PySide6"])


def _config(backend: str = "faster-whisper", model: str = "tiny", vosk_path: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        speech=SimpleNamespace(
            stt_backend=backend, whisper_model=model, vosk_model_path=vosk_path
        )
    )


class SpeechModelReadyTests(unittest.TestCase):
    def test_vosk_requires_an_existing_model_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope")
            self.assertFalse(ensure_components.speech_model_ready(_config("vosk", "", missing)))
            self.assertTrue(ensure_components.speech_model_ready(_config("vosk", "", tmp)))

    def test_local_directory_model_counts_as_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(ensure_components.speech_model_ready(_config(model=tmp)))

    def test_huggingface_cache_requires_config_and_weights(self) -> None:
        fake_hub = types.ModuleType("huggingface_hub")
        calls: list[tuple[str, str]] = []

        def cached(repo: str, filename: str) -> str:
            calls.append((repo, filename))
            return f"/cache/{repo}/{filename}"

        fake_hub.try_to_load_from_cache = cached  # type: ignore[attr-defined]
        with (
            patch.object(ensure_components, "module_installed", return_value=True),
            patch.dict(sys.modules, {"huggingface_hub": fake_hub}),
        ):
            ready = ensure_components.speech_model_ready(_config())
        self.assertTrue(ready)
        self.assertEqual(
            calls,
            [
                ("Systran/faster-whisper-tiny", "config.json"),
                ("Systran/faster-whisper-tiny", "model.bin"),
            ],
        )

    def test_half_finished_download_is_not_ready(self) -> None:
        fake_hub = types.ModuleType("huggingface_hub")
        fake_hub.try_to_load_from_cache = (  # type: ignore[attr-defined]
            lambda repo, filename: "/cache/config.json" if filename == "config.json" else None
        )
        with (
            patch.object(ensure_components, "module_installed", return_value=True),
            patch.dict(sys.modules, {"huggingface_hub": fake_hub}),
        ):
            self.assertFalse(ensure_components.speech_model_ready(_config()))

    def test_named_huggingface_repo_is_used_verbatim(self) -> None:
        fake_hub = types.ModuleType("huggingface_hub")
        calls: list[tuple[str, str]] = []
        fake_hub.try_to_load_from_cache = (  # type: ignore[attr-defined]
            lambda repo, filename: calls.append((repo, filename)) or "/cache/x"
        )
        with (
            patch.object(ensure_components, "module_installed", return_value=True),
            patch.dict(sys.modules, {"huggingface_hub": fake_hub}),
        ):
            self.assertTrue(
                ensure_components.speech_model_ready(_config(model="org/custom-whisper"))
            )
        self.assertEqual(calls[0][0], "org/custom-whisper")


class InstallComponentsTests(unittest.TestCase):
    def test_vosk_configuration_installs_the_light_speech_extra(self) -> None:
        result = SimpleNamespace(returncode=0)
        with (
            patch.object(ensure_components.subprocess, "run", return_value=result) as run,
            patch.object(ensure_components, "missing_components", return_value=[]),
        ):
            self.assertEqual(ensure_components.install_components(_config("vosk")), 0)
        command = run.call_args.args[0]
        self.assertIn(".[gui,tts,windows-audio,light-speech]", command)


class MainFlowTests(unittest.TestCase):
    def test_main_succeeds_when_components_and_model_are_ready(self) -> None:
        with (
            patch.object(ensure_components, "missing_components", return_value=[]),
            patch.object(ensure_components, "speech_model_ready", return_value=True),
            patch.object(ensure_components, "load_config", return_value=_config()),
        ):
            self.assertEqual(ensure_components.main(["--skip-model"]), 0)

    def test_main_installs_when_components_are_missing(self) -> None:
        with (
            patch.object(
                ensure_components, "missing_components", side_effect=[["PySide6"], []]
            ),
            patch.object(ensure_components, "install_components", return_value=0) as install,
            patch.object(ensure_components, "load_config", return_value=_config()),
            patch.object(ensure_components, "speech_model_ready", return_value=True),
        ):
            self.assertEqual(ensure_components.main([]), 0)
        install.assert_called_once()

    def test_main_reports_failed_install(self) -> None:
        with (
            patch.object(ensure_components, "missing_components", return_value=["PySide6"]),
            patch.object(ensure_components, "install_components", return_value=1),
            patch.object(ensure_components, "load_config", return_value=_config()),
        ):
            self.assertEqual(ensure_components.main([]), 1)


if __name__ == "__main__":
    unittest.main()
