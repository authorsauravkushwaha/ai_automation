import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = os.environ.copy()
        environment["HINATA_DATA_DIR"] = str(Path(temporary.name) / "data")
        return subprocess.run(
            [sys.executable, "-m", "hinata", "--no-llm", *arguments],
            input=input_text,
            text=True,
            capture_output=True,
            timeout=20,
            cwd=Path(__file__).resolve().parent.parent,
            env=environment,
            check=False,
        )

    def test_one_shot_system_info(self) -> None:
        result = self.run_cli("ask", "system", "info")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("logical_cpus", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_noninteractive_approval_eof_cancels_cleanly(self) -> None:
        result = self.run_cli("ask", "close", "notepad")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cancelled", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_unknown_request_does_not_need_network_in_no_llm_mode(self) -> None:
        result = self.run_cli("ask", "tell", "me", "a", "joke")
        self.assertEqual(result.returncode, 0)
        self.assertIn("clearer request", result.stdout)


if __name__ == "__main__":
    unittest.main()
