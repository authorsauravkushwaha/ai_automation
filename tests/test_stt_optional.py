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


if __name__ == "__main__":
    unittest.main()
