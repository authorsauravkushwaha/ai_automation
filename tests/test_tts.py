import sys
import threading
import types
import unittest
from unittest.mock import patch

from hinata.config import SpeechConfig
from hinata.speech.text_to_speech import TextToSpeech


class FakeEngine:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stopped = False
        self.run_started = threading.Event()
        self.release = threading.Event()

    def setProperty(self, *args) -> None:
        pass

    def say(self, text: str) -> None:
        self.spoken.append(text)

    def runAndWait(self) -> None:
        self.run_started.set()
        self.release.wait(2)

    def stop(self) -> None:
        self.stopped = True
        self.release.set()


class SpeechOutputTests(unittest.TestCase):
    def test_cancel_during_engine_initialization_never_speaks(self) -> None:
        engine = FakeEngine()
        initialized = threading.Event()
        release_init = threading.Event()

        def initialize(*args) -> FakeEngine:
            initialized.set()
            release_init.wait(2)
            return engine

        module = types.SimpleNamespace(init=initialize)
        tts = TextToSpeech(SpeechConfig())
        with patch.dict(sys.modules, {"pyttsx3": module}):
            tts.speak("old response")
            self.assertTrue(initialized.wait(1))
            tts.stop()
            release_init.set()
            tts.wait(2)
        self.assertEqual(engine.spoken, [])
        self.assertFalse(tts.speaking)

    def test_active_speech_is_interruptible(self) -> None:
        engine = FakeEngine()
        module = types.SimpleNamespace(init=lambda *args: engine)
        tts = TextToSpeech(SpeechConfig())
        with patch.dict(sys.modules, {"pyttsx3": module}):
            tts.speak("interrupt this")
            self.assertTrue(engine.run_started.wait(1))
            tts.stop()
            tts.wait(2)
        self.assertTrue(engine.stopped)
        self.assertEqual(engine.spoken, ["interrupt this"])
        self.assertFalse(tts.speaking)

    def test_normal_completion_callbacks(self) -> None:
        engine = FakeEngine()
        engine.release.set()
        states: list[str] = []
        module = types.SimpleNamespace(init=lambda *args: engine)
        tts = TextToSpeech(SpeechConfig())
        with patch.dict(sys.modules, {"pyttsx3": module}):
            tts.speak("hello", lambda state, error: states.append(state))
            tts.wait(2)
        self.assertEqual(states, ["speaking", "idle"])
        self.assertFalse(tts.speaking)


if __name__ == "__main__":
    unittest.main()
