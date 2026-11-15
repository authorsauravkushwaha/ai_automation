import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hinata.core.types import ActionRequest, AssistantResponse
from hinata.main import desktop_import_error, desktop_status, emit_with_approvals


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

    def test_desktop_diagnostic_distinguishes_missing_package_from_native_library(self) -> None:
        missing = ModuleNotFoundError("No module named 'PySide6'", name="PySide6")
        self.assertIn('pip install -e ".[gui]"', desktop_import_error(missing))
        with patch("hinata.main.sys.platform", "linux"):
            message = desktop_import_error(ImportError("libGL.so.1: cannot open shared object file"))
        self.assertIn("apt-get install libgl1", message)
        self.assertNotIn("pip install", message)

    def test_desktop_status_probes_qt_import(self) -> None:
        with patch("hinata.main.importlib.util.find_spec", return_value=None):
            self.assertEqual(desktop_status(), "not installed")
        with patch("hinata.main.importlib.util.find_spec", return_value=object()), patch(
            "hinata.main.importlib.import_module", side_effect=ImportError("missing native library")
        ):
            self.assertIn("missing native library", desktop_status())
        with patch("hinata.main.importlib.util.find_spec", return_value=object()), patch(
            "hinata.main.importlib.import_module", return_value=object()
        ):
            self.assertIn("display not tested", desktop_status())

    def test_confirmation_displays_model_selected_arguments(self) -> None:
        assistant = SimpleNamespace()
        action = ActionRequest(tool="remember_preference", arguments={"key": "theme", "value": "violet"})
        response = AssistantResponse("Approval required", False, "confirmation_required", action)
        output = io.StringIO()
        with patch("builtins.input", return_value="no"), contextlib.redirect_stdout(output):
            result = emit_with_approvals(assistant, response)
        self.assertEqual(result, 1)
        self.assertIn('"value": "violet"', output.getvalue())
        self.assertIn("remember_preference", output.getvalue())

    def test_one_shot_speech_waits_and_reports_failures(self) -> None:
        tts = Mock()
        tts.last_error = None
        assistant = SimpleNamespace(tts=tts)
        response = AssistantResponse("hello", True, "completed")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(emit_with_approvals(assistant, response, speak=True, wait_for_speech=True), 0)
        tts.wait.assert_called_once()
        tts.last_error = "device unavailable"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(emit_with_approvals(assistant, response, speak=True, wait_for_speech=True), 1)
        self.assertIn("device unavailable", error.getvalue())

    def test_agentic_cli_subcommands_run_offline(self) -> None:
        hw = self.run_cli("model", "hardware")
        self.assertEqual(hw.returncode, 0, hw.stderr)
        self.assertIn("recommended_profile", hw.stdout)

        agents = self.run_cli("agents")
        self.assertEqual(agents.returncode, 0, agents.stderr)
        self.assertIn("Planner Agent", agents.stdout)
        self.assertIn("Reviewer & QA Agent", agents.stdout)

        ws = self.run_cli("workspaces")
        self.assertEqual(ws.returncode, 0, ws.stderr)
        self.assertEqual(ws.stdout.strip(), "[]")

        assets = self.run_cli("assets")
        self.assertEqual(assets.returncode, 0, assets.stderr)
        self.assertIn("registered_assets", assets.stdout)

        audit = self.run_cli("audit")
        self.assertEqual(audit.returncode, 0, audit.stderr)
        self.assertIn("recent_trace_events", audit.stdout)

    def test_voice_diagnostics_barge_in_and_vision_capability_gating(self) -> None:
        from hinata.automation.app_control import AutomationError
        from hinata.automation.file_manager import FileManager
        from hinata.config import SecurityConfig, SpeechConfig
        from hinata.speech.multimodal import LocalVisionAdapter, VoiceDiagnostics

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        mic = Mock()
        mic.list_devices.return_value = [{"index": 0, "name": "Local Mic"}]
        stt = Mock()
        tts = Mock()
        speaker = Mock()
        speaker.is_enrolled = False

        diag = VoiceDiagnostics(SpeechConfig(), mic, stt, tts, speaker)
        status = diag.status()
        self.assertIn("Probabilistic convenience check only", status["speaker_note"])
        self.assertEqual(status["input_devices"][0]["name"], "Local Mic")
        diag.barge_in_stop()
        tts.stop.assert_called_once()

        # Vision capability gating fails closed honestly when no offline OCR or vision model exists
        files = FileManager(SecurityConfig(allowed_roots=(root,)))
        img = root / "sample.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        vision = LocalVisionAdapter(files, llm=None)
        with patch("hinata.speech.multimodal.importlib.util.find_spec", return_value=None):
            caps = vision.capabilities()
            self.assertFalse(caps["multimodal_ready"])
            with self.assertRaises(AutomationError):
                vision.inspect_image(str(img))


if __name__ == "__main__":
    unittest.main()
