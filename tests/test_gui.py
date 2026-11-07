import importlib.util
import os
import tempfile
import unittest

from tests.helpers import make_assistant

GUI_IMPORT_ERROR = ""
try:
    if importlib.util.find_spec("PySide6") is None:
        raise ImportError("PySide6 optional dependency is not installed")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from friday.interface.main_window import MainWindow
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

    def tearDown(self) -> None:
        self.window.close()
        self.assistant.close()
        self.temporary.cleanup()

    def test_required_desktop_controls_exist(self) -> None:
        self.assertIsNotNone(self.window.microphone_button)
        self.assertIsNotNone(self.window.input)
        self.assertIsNotNone(self.window.conversation)
        self.assertIsNotNone(self.window.listening_pill)
        self.assertIsNotNone(self.window.processing_pill)
        self.assertIsNotNone(self.window.speaking_pill)

    def test_conversation_escapes_model_html(self) -> None:
        self.window.conversation.add_message("FRIDAY", "<img src=x onerror=alert(1)>")
        self.assertIn("<img src=x onerror=alert(1)>", self.window.conversation.toPlainText())
        self.assertNotIn("<img src=x onerror=alert(1)>", self.window.conversation.toHtml())

    def test_settings_default_to_local_values(self) -> None:
        self.assertFalse(self.window.speak_responses)
        self.assertFalse(self.assistant.orchestrator.llm_enabled)


if __name__ == "__main__":
    unittest.main()
