import importlib.util
import sys
import types
import unittest
from unittest.mock import patch

from hinata.config import SpeechConfig
from hinata.speech.speech_to_text import SpeechToText


@unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional dependency is not installed")
class VoskStateTests(unittest.TestCase):
    def test_model_is_cached_but_recognizer_state_is_reset(self) -> None:
        import numpy as np

        models = []
        recognizers = []

        class FakeModel:
            def __init__(self, path: str) -> None:
                models.append(path)

        class FakeRecognizer:
            def __init__(self, model: FakeModel, sample_rate: int) -> None:
                recognizers.append(sample_rate)

            def AcceptWaveform(self, pcm: bytes) -> bool:
                return True

            def FinalResult(self) -> str:
                return '{"text":"local command"}'

        with patch.dict(
            sys.modules,
            {"vosk": types.SimpleNamespace(Model=FakeModel, KaldiRecognizer=FakeRecognizer)},
        ):
            stt = SpeechToText(SpeechConfig(stt_backend="vosk", vosk_model_path="."))
            audio = np.zeros(16000, dtype="float32")
            self.assertEqual(stt.transcribe(audio), "local command")
            self.assertEqual(stt.transcribe(audio), "local command")
        self.assertEqual(len(models), 1)
        self.assertEqual(len(recognizers), 2)


class WhisperOfflineTests(unittest.TestCase):
    def test_requested_whisper_languages_are_passed_to_local_model(self) -> None:
        calls = []
        class FakeModel:
            def transcribe(self, audio, **kwargs):
                calls.append(kwargs["language"])
                return [types.SimpleNamespace(text=" hello ")], None
        for language in ("en", "hi", "fr", "de"):
            config = SpeechConfig(language=language)
            config.validate()
            stt = SpeechToText(config)
            stt._model = FakeModel()
            self.assertEqual(stt._transcribe_whisper(object()), "hello")
        self.assertEqual(calls, ["en", "hi", "fr", "de"])
        with self.assertRaises(ValueError):
            SpeechConfig(language="xx").validate()

    def test_only_explicit_setup_allows_downloading(self) -> None:
        calls = []

        def fake_model(*args, **kwargs):
            calls.append(kwargs)
            return object()

        with patch.dict(sys.modules, {"faster_whisper": types.SimpleNamespace(WhisperModel=fake_model)}):
            stt = SpeechToText(SpeechConfig())
            stt._load_whisper()
            stt.unload()
            stt.prepare()
        self.assertTrue(calls[0]["local_files_only"])
        self.assertFalse(calls[1]["local_files_only"])

    def test_voice_pipeline_unloads_stt_after_success_and_failure(self) -> None:
        from hinata.speech.speaker_recognition import SpeakerVerification
        from hinata.speech.voice_pipeline import VoicePipeline

        class FakeSTT:
            def __init__(self):
                self.unloaded = 0
                self.fail = False

            def transcribe(self, audio, rate):
                if self.fail:
                    raise RuntimeError("failure")
                return "hello"

            def unload(self):
                self.unloaded += 1

        stt = FakeSTT()
        speaker = types.SimpleNamespace(verify=lambda audio: SpeakerVerification("not_enrolled", None, None, False))
        pipeline = VoicePipeline(types.SimpleNamespace(sample_rate=16000), stt, speaker)
        self.assertEqual(pipeline.process_audio(None).text, "hello")
        stt.fail = True
        with self.assertRaises(RuntimeError):
            pipeline.process_audio(None)
        self.assertEqual(stt.unloaded, 2)


if __name__ == "__main__":
    unittest.main()
