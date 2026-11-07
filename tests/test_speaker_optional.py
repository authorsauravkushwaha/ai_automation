import importlib.util
import tempfile
import unittest
from pathlib import Path

from hinata.config import SpeechConfig
from hinata.speech.speaker_recognition import SpeakerRecognition


@unittest.skipUnless(importlib.util.find_spec("numpy"), "NumPy optional dependency is not installed")
class SpeakerProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        import numpy as np

        self.np = np
        self.temporary = tempfile.TemporaryDirectory()
        self.speaker = SpeakerRecognition(SpeechConfig(), Path(self.temporary.name))
        # Deterministic unit embeddings avoid loading PyTorch in this storage/policy test.
        self.speaker._embed = lambda audio: np.asarray(audio, dtype="float32")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_enroll_verify_uncertain_reject_and_delete(self) -> None:
        samples = [self.np.array([1.0, 0.0]) for _ in range(3)]
        result = self.speaker.enroll(samples)
        self.assertEqual(result["samples"], 3)
        self.assertTrue(self.speaker.is_enrolled)
        self.assertEqual(self.speaker.verify(samples[0]).status, "owner")
        uncertain = self.speaker.verify(self.np.array([0.76, (1 - 0.76**2) ** 0.5]))
        self.assertEqual(uncertain.status, "uncertain")
        self.assertFalse(uncertain.match)
        rejected = self.speaker.verify(self.np.array([0.0, 1.0]))
        self.assertEqual(rejected.status, "rejected")
        self.assertTrue(self.speaker.delete_profile())
        self.assertFalse(self.speaker.is_enrolled)


if __name__ == "__main__":
    unittest.main()
