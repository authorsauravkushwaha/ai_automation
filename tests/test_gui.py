import importlib.util
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.helpers import make_assistant

GUI_IMPORT_ERROR = ""
try:
    if importlib.util.find_spec("PySide6") is None:
        raise ImportError("PySide6 optional dependency is not installed")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from hinata.interface.main_window import MainWindow
except ImportError as exc:
    QApplication = None
    MainWindow = None
    GUI_IMPORT_ERROR = str(exc)


@unittest.skipIf(QApplication is None, GUI_IMPORT_ERROR)
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.temporary.name)
        self.window = MainWindow(self.assistant)

    @classmethod
    def tearDownClass(cls) -> None:
        QApplication.closeAllWindows()
        cls.application.processEvents()
        cls.application.quit()
        # Release the wrapper before Python unloads Qt modules at interpreter exit.
        cls.application = None

    def tearDown(self) -> None:
        self.window.close()
        self.window.deleteLater()
        QApplication.processEvents()
        self.assistant.close()
        self.temporary.cleanup()

    def test_required_desktop_controls_exist(self) -> None:
        self.assertIsNotNone(self.window.microphone_button)
        self.assertIsNotNone(self.window.input)
        self.assertIsNotNone(self.window.conversation)
        self.assertIsNotNone(self.window.listening_pill)
        self.assertIsNotNone(self.window.processing_pill)
        self.assertIsNotNone(self.window.speaking_pill)
        self.assertEqual(self.window.mode_combo.count(), 6)
        self.assertEqual(self.window.stop_button.text(), "Emergency stop")

    def test_task_review_requires_user_confirmation_and_evidence(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        self.assistant.orchestrator.llm_enabled = True
        self.assistant.llm.complete = lambda *args, **kwargs: (
            '{"type":"plan","objective":"Make folder",'
            '"steps":[{"tool":"create_folder","arguments":{"path":"Documents/test-review"}}]}'
        )
        self.window.speak_responses = False  # This test verifies review, not the audio backend.
        proposed = self.assistant.process_text("please prepare the workspace")
        result = self.assistant.execute_plan(proposed.plan_actions, task_id=proposed.data["task_id"])
        task_id = result.data["task_id"]
        with patch("hinata.interface.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            self.window.handle_response(result)
        self.assertEqual(self.assistant.tasks.get(task_id)["state"], "VERIFYING")
        with patch("hinata.interface.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), patch(
            "hinata.interface.main_window.QInputDialog.getMultiLineText", return_value=("I inspected the requested folder.", True)
        ):
            self.window.handle_response(result)
        self.assertEqual(self.assistant.tasks.get(task_id)["state"], "COMPLETED")

    def test_conversation_escapes_model_html(self) -> None:
        self.window.conversation.add_message("HINATA", "<img src=x onerror=alert(1)>")
        self.assertIn("<img src=x onerror=alert(1)>", self.window.conversation.toPlainText())
        self.assertNotIn("<img src=x onerror=alert(1)>", self.window.conversation.toHtml())

    def test_typed_and_voice_replies_speak_once_and_do_not_read_approvals(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from hinata.core.types import ActionRequest, AssistantResponse

        voice = SimpleNamespace(speaker_match=True, speaker_required=False)
        with patch.object(self.window, "speak_text") as speak:
            self.window.handle_response(AssistantResponse("Typed answer", True))
            speak.assert_called_once_with("Typed answer")
            speak.reset_mock()
            self.window.handle_response(AssistantResponse("Voice answer", True), voice=voice)
            speak.assert_called_once_with("Voice answer")
        from PySide6.QtWidgets import QMessageBox
        with patch.object(self.window, "speak_text") as speak, patch(
            "hinata.interface.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.No,
        ):
            self.window.handle_response(AssistantResponse(
                "secret argument", False, "confirmation_required",
                ActionRequest("close_application", {"name": "secret.exe"}),
            ))
            self.assertEqual(speak.call_count, 2)
            self.assertNotIn("secret", " ".join(call.args[0] for call in speak.call_args_list))

    def test_settings_default_to_local_values(self) -> None:
        self.assertTrue(self.window.speak_responses)
        self.assertFalse(self.assistant.orchestrator.llm_enabled)


if __name__ == "__main__":
    unittest.main()
