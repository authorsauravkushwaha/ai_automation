import os
import tempfile
import threading
import unittest
from pathlib import Path

from tests.helpers import make_assistant


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.temporary.name)
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.assistant.close()
        self.temporary.cleanup()

    def test_preference_requires_explicit_approval(self) -> None:
        response = self.assistant.process_text("remember that my editor is VS Code")
        self.assertTrue(response.needs_confirmation)
        self.assertEqual(self.assistant.memory.preferences(), [])
        result = self.assistant.resume(response.action)
        self.assertTrue(result.success)
        self.assertEqual(self.assistant.memory.preferences()[0]["value"], "VS Code")

    def test_sensitive_clear_requires_confirmation_and_correct_pin(self) -> None:
        self.assistant.authentication.set_pin("1234")
        self.assistant.memory.set_preference("editor", "VS Code")
        response = self.assistant.process_text("clear memory")
        self.assertTrue(response.needs_confirmation)
        response = self.assistant.resume(response.action)
        self.assertTrue(response.needs_pin)
        denied = self.assistant.resume(response.action, pin="9999")
        self.assertEqual(denied.status, "denied")
        result = self.assistant.resume(response.action, pin="1234")
        self.assertTrue(result.success, result.text)
        self.assertEqual(self.assistant.memory.preferences(), [])

    def test_clear_without_configured_pin_fails_safely(self) -> None:
        response = self.assistant.process_text("clear preferences")
        response = self.assistant.resume(response.action)
        self.assertEqual(response.status, "denied")
        self.assertIn("setup-pin", response.text)

    def test_uncertain_enrolled_voice_is_denied(self) -> None:
        response = self.assistant.orchestrator.process(
            "system info", source="voice", speaker_match=None, speaker_required=True
        )
        self.assertEqual(response.status, "denied")
        self.assertFalse(response.success)

    def test_real_folder_creation_is_verified(self) -> None:
        response = self.assistant.process_text("create folder Documents/HINATA-Test")
        self.assertTrue(response.success, response.text)
        self.assertTrue((self.root / "Documents" / "HINATA-Test").is_dir())

    def test_read_content_is_not_duplicated_into_task_results(self) -> None:
        note = self.root / "Documents" / "note.txt"
        note.write_text("private document body", encoding="utf-8")
        response = self.assistant.process_text("read text file Documents/note.txt")
        self.assertTrue(response.success)
        row = self.assistant.database.fetch_one("SELECT result_json FROM task_results ORDER BY id DESC")
        self.assertNotIn("private document body", row["result_json"])
        self.assertIn("content omitted", row["result_json"])

    @unittest.skipIf(os.name == "nt", "Do not launch Notepad during portable tests")
    def test_unsupported_platform_action_reports_failure(self) -> None:
        response = self.assistant.process_text("open notepad")
        self.assertFalse(response.success)
        self.assertIn("only on Windows", response.text)
        command = self.assistant.database.fetch_one("SELECT status FROM commands ORDER BY id DESC")
        self.assertEqual(command["status"], "failed")

    def test_model_chat_response(self) -> None:
        self.assistant.orchestrator.llm_enabled = True
        self.assistant.llm.complete = lambda *args: '{"type":"chat","content":"Local hello"}'
        response = self.assistant.process_text("hello there")
        self.assertEqual(response.text, "Local hello")
        self.assertTrue(response.success)

    def test_model_proposed_tool_still_needs_approval(self) -> None:
        self.assistant.orchestrator.llm_enabled = True
        self.assistant.llm.complete = lambda *args: (
            '{"type":"tool","tool":"remember_preference",'
            '"arguments":{"key":"theme","value":"dark"}}'
        )
        response = self.assistant.process_text("Please save my theme")
        self.assertTrue(response.needs_confirmation)
        self.assertEqual(self.assistant.memory.preferences(), [])

    def test_model_plan_waits_for_owner_approval_then_executes_verified_safe_steps(self) -> None:
        self.assistant.orchestrator.llm_enabled = True
        self.assistant.llm.complete = lambda *args: (
            '{"type":"plan","objective":"Create a work folder",'
            '"steps":[{"tool":"create_folder","arguments":{"path":"Documents/Approved-Plan"}}]}'
        )
        pending = self.assistant.process_text("please set up a work folder")
        self.assertTrue(pending.needs_plan_confirmation)
        self.assertFalse((self.root / "Documents" / "Approved-Plan").exists())
        result = self.assistant.execute_plan(pending.plan_actions)
        self.assertTrue(result.success, result.text)
        self.assertTrue((self.root / "Documents" / "Approved-Plan").is_dir())

    def test_emergency_stop_prevents_an_unfinished_model_plan_from_running(self) -> None:
        self.assistant.orchestrator.llm_enabled = True
        entered_model = threading.Event()
        release_model = threading.Event()

        def slow_model(*args: object) -> str:
            entered_model.set()
            release_model.wait(timeout=2)
            return '{"type":"tool","tool":"create_folder","arguments":{"path":"stopped"}}'

        self.assistant.llm.complete = slow_model
        responses = []
        worker = threading.Thread(
            target=lambda: responses.append(self.assistant.process_text("please organize my workspace"))
        )
        worker.start()
        self.assertTrue(entered_model.wait(timeout=1))
        self.assistant.emergency_stop()
        release_model.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(responses[0].status, "cancelled")
        self.assertFalse((self.root / "Documents" / "stopped").exists())


if __name__ == "__main__":
    unittest.main()
