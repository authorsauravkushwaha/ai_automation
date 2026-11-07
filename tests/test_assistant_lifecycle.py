import logging
import tempfile
import unittest

from tests.helpers import make_assistant


class AssistantLifecycleTests(unittest.TestCase):
    def test_close_releases_log_handler_and_allows_data_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            assistant = make_assistant(directory)
            handler = assistant._log_handler
            self.assertIsNotNone(handler)
            self.assertIn(handler, logging.getLogger("friday").handlers)
            assistant.close()
            self.assertNotIn(handler, logging.getLogger("friday").handlers)
            assistant.close()  # Idempotent shutdown is used by both front ends.
        # On Windows this context exit fails if friday.log remains open.


if __name__ == "__main__":
    unittest.main()
