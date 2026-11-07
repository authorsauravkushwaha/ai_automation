import importlib.util
import sys
import threading
import types
import unittest
from unittest.mock import patch

from friday.speech.wake_word import WakeWordService


@unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional dependency is not installed")
class WakeWordServiceTests(unittest.TestCase):
    def test_bounded_stream_detection_and_clean_restart(self) -> None:
        import numpy as np

        streams = []

        class FakeStream:
            def __init__(self, **kwargs) -> None:
                self.callback = kwargs["callback"]
                self.started = False
                streams.append(self)

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.started = False

            def close(self) -> None:
                pass

        class FakeDetector:
            def process(self, chunk):
                return True, 0.91

        detected = threading.Event()
        scores = []
        module = types.SimpleNamespace(InputStream=FakeStream)
        service = WakeWordService(FakeDetector())
        with patch.dict(sys.modules, {"sounddevice": module}):
            service.start(lambda score: (scores.append(score), detected.set()))
            streams[0].callback(
                np.zeros((1280, 1), dtype="int16"), 1280, None, None
            )
            self.assertTrue(detected.wait(1))
            service.stop()
            self.assertFalse(service.running)
            detected.clear()
            service.start(lambda score: detected.set())
            service.stop()
        self.assertEqual(scores, [0.91])
        self.assertEqual(len(streams), 2)


if __name__ == "__main__":
    unittest.main()
