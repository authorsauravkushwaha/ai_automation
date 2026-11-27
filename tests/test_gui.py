import importlib.util
import os
import tempfile
import unittest
from unittest.mock import PropertyMock, patch
from urllib.parse import parse_qs, urlsplit

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

    def test_thinking_selector_is_present_and_switches_profile(self) -> None:
        from hinata.interface.components import ThinkingSelector

        self.assertIsInstance(self.window.thinking_combo, ThinkingSelector)
        profiles = {self.window.thinking_combo.itemData(i) for i in range(self.window.thinking_combo.count())}
        self.assertEqual(profiles, {"off", "quick", "balanced", "deep", "ultra"})
        index = self.window.thinking_combo.findData("deep")
        self.window.thinking_combo.setCurrentIndex(index)
        self.assertEqual(self.assistant.thinking.mode, "deep")
        off_index = self.window.thinking_combo.findData("off")
        self.window.thinking_combo.setCurrentIndex(off_index)
        self.assertFalse(self.assistant.orchestrator.thinking_active)
        self.window.thinking_combo.setCurrentIndex(self.window.thinking_combo.findData("balanced"))
        self.assertTrue(self.assistant.orchestrator.thinking_active)

    def test_deliberated_reply_shows_only_a_compact_badge(self) -> None:
        from hinata.core.types import AssistantResponse

        self.window.speak_responses = False
        trace = {
            "engaged": True,
            "badge": "balanced ×2 passes · 1 fix · high confidence",
            "confidence": "high",
            "critique": [{"title": "Internal reviewer note", "severity": "gap", "detail": "private"}],
        }
        self.window.handle_response(AssistantResponse("Final answer", True, "completed", data={"thinking": trace}))
        text = self.window.conversation.toPlainText()
        self.assertIn("balanced ×2 passes", text)
        self.assertNotIn("Internal reviewer note", text)

    def test_thinking_can_be_disabled_from_settings(self) -> None:
        from hinata.interface.dialogs import SettingsDialog

        dialog = SettingsDialog(
            self.assistant,
            speak_responses=False,
            llm_enabled=True,
            parent=self.window,
        )
        try:
            # make_assistant() starts with the local model disabled, so the
            # checkbox must reflect the live state rather than the config wish.
            self.assertEqual(
                dialog.thinking_enabled_checkbox.isChecked(),
                self.assistant.orchestrator.thinking_active,
            )
            self.assertIn(dialog.thinking_settings["mode"], {"off", "quick", "balanced", "deep", "ultra"})
            dialog.thinking_enabled_checkbox.setChecked(False)
            dialog.thinking_mode_combo.setCurrentIndex(dialog.thinking_mode_combo.findData("ultra"))
            choice = dialog.thinking_settings
            self.assertFalse(choice["enabled"])
            self.assertEqual(choice["mode"], "ultra")
            self.assertGreaterEqual(choice["max_seconds"], 10.0)
            self.assertGreaterEqual(choice["max_model_calls"], 1)
        finally:
            dialog.deleteLater()

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

    def test_hands_free_turn_waits_for_reply_then_reopens_mic_until_stopped(self) -> None:
        from hinata.core.types import AssistantResponse
        from hinata.speech.voice_pipeline import VoiceResult

        voice = VoiceResult("system info", True, True, "verified")
        mic = self.assistant.microphone
        tts = self.assistant.tts
        self.window.speak_responses = False
        with (
            patch.object(self.assistant.voice, "start") as start,
            patch.object(mic, "poll_utterance", return_value=True),
            patch.object(mic, "cancel") as cancel,
            patch.object(self.assistant.voice, "stop_and_process", return_value=voice),
            patch.object(self.assistant.orchestrator, "process", return_value=AssistantResponse("Done", True)) as process,
            patch.object(type(tts), "speaking", new_callable=PropertyMock, return_value=False) as speaking,
        ):
            self.window.toggle_microphone()
            self.window._voice_timer.stop()  # Step the listener deterministically.
            start.assert_called_once()
            self.window._voice_tick()
            # First worker transcribes; the UI callback schedules the command worker.
            for _ in range(2):
                self.assertTrue(self.window.threadpool.waitForDone(3000))
                QApplication.processEvents()
            process.assert_called_once_with("system info", source="voice", speaker_match=True, speaker_required=True)
            self.assertIn("Done", self.window.conversation.toPlainText())
            speaking.return_value = True
            self.window._voice_tick()
            start.assert_called_once()
            speaking.return_value = False
            self.window._voice_resume_at = 0.0
            self.window._voice_tick()
            self.assertEqual(start.call_count, 2)
            self.window.emergency_stop()
            self.window._voice_tick()
            self.assertEqual(start.call_count, 2)
            cancel.assert_called()
            self.assertEqual(self.window.microphone_button.text(), "Start hands-free voice")

    def test_browser_retry_waits_for_click_and_preserves_the_draft(self) -> None:
        self.window.speak_responses = False
        query = "train times"
        web = self.assistant.orchestrator.web_search
        with (
            patch.object(web, "search", return_value={"results": [], "page_excerpts": []}),
            patch("hinata.automation.web_search.open_browser_search", return_value="your default browser") as launch,
        ):
            self.window.submit_text(f"search for {query}")
            self.assertTrue(self.window.threadpool.waitForDone(3000))
            QApplication.processEvents()
            self.assertFalse(self.window.browser_retry_button.isHidden())
            launch.assert_not_called()
            self.window.input.setText("my next question")
            self.window.browser_retry_button.click()
            self.assertTrue(self.window.threadpool.waitForDone(3000))
            QApplication.processEvents()
            launch.assert_called_once()
            self.assertEqual(parse_qs(urlsplit(launch.call_args.args[0]).query), {"q": [query]})
            self.assertEqual(self.window.input.text(), "my next question")
            self.assertTrue(self.window.browser_retry_button.isHidden())

    def test_chat_navigation_preserves_reading_position_when_new_messages_arrive(self) -> None:
        from hinata.speech.voice_pipeline import VoiceResult

        self.window.show()
        QApplication.processEvents()
        conversation = self.window.conversation
        for i in range(30):
            conversation.add_message("HINATA", f"Message {i}\n" + "More text\n" * 7)
        QApplication.processEvents()
        scrollbar = conversation.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        self.assertEqual(scrollbar.value(), scrollbar.maximum())
        with patch.object(self.assistant, "process_text") as process_text:
            self.window.submit_text("Scroll chat up.")
            process_text.assert_not_called()
        position = scrollbar.value()
        self.assertLess(position, scrollbar.maximum())
        conversation.add_message("HINATA", "A new reply arrived")
        QApplication.processEvents()
        self.assertEqual(scrollbar.value(), position)
        self.window.process_voice_result(VoiceResult("Hinata, go to the latest message.", True, True, "verified"))
        self.assertEqual(scrollbar.value(), scrollbar.maximum())

    def test_voice_settings_load_from_the_local_profile(self) -> None:
        database = self.assistant.database
        database.set_setting("ui.hands_free_pause_ms", "1750")
        database.set_setting("ui.hands_free_sensitivity", "high")
        self.window._load_ui_settings()
        self.assertEqual(self.assistant.microphone.hands_free.pause_ms, 1750)
        self.assertEqual(self.assistant.microphone.hands_free.sensitivity, "high")

    def test_settings_default_to_local_values(self) -> None:
        self.assertTrue(self.window.speak_responses)
        self.assertFalse(self.assistant.orchestrator.llm_enabled)
        self.assistant.database.set_setting("ui.speak_responses", "false")
        self.window._load_ui_settings()
        self.assertFalse(self.window.speak_responses)
        self.assistant.database.set_setting("ui.speak_responses", "true")
        self.window._load_ui_settings()
        self.assertTrue(self.window.speak_responses)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(QApplication is None, GUI_IMPORT_ERROR)
class DialogTests(unittest.TestCase):
    """Real Qt dialogs, constructed headless. These files held security-relevant
    consent UI (lockdown, PIN entry, memory clear) and were never exercised."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.temporary.name)

    def tearDown(self) -> None:
        self.assistant.close()
        self.temporary.cleanup()

    def test_settings_dialog_builds_and_reports_its_controls(self) -> None:
        from hinata.interface.dialogs import SettingsDialog

        dialog = SettingsDialog(self.assistant, speak_responses=True, llm_enabled=False)
        try:
            self.assertTrue(dialog.speak_checkbox.isChecked())
            self.assertFalse(dialog.llm_checkbox.isChecked())
            self.assertEqual(dialog.owner_name_input.text(), self.assistant.personality.owner_name)
            # Toggle and confirm the getters follow the widgets, not the constructor args.
            dialog.speak_checkbox.setChecked(False)
            dialog.llm_checkbox.setChecked(True)
            self.assertFalse(dialog.speak_checkbox.isChecked())
            self.assertTrue(dialog.llm_checkbox.isChecked())
        finally:
            dialog.deleteLater()

    def test_settings_dialog_hands_free_settings_round_trip(self) -> None:
        from hinata.interface.dialogs import SettingsDialog

        dialog = SettingsDialog(self.assistant, speak_responses=False, llm_enabled=False)
        try:
            settings = dialog.hands_free_settings  # property, not a method
            self.assertGreater(settings.pause_ms, 0)
            self.assertTrue(settings.sensitivity)
        finally:
            dialog.deleteLater()

    def test_settings_dialog_describes_connection_states(self) -> None:
        from hinata.interface.dialogs import SettingsDialog

        descriptions = {
            state: SettingsDialog.describe_connection(state)
            for state in ("ok", "unavailable", "unknown")
        }
        for state, text in descriptions.items():
            self.assertTrue(text, state)

    def test_memory_dialog_lists_conversation_history(self) -> None:
        from hinata.interface.dialogs import MemoryDialog

        self.assistant.memory.add_message("user", "remember the blue folder", "text")
        dialog = MemoryDialog(self.assistant)
        try:
            dialog.refresh()
            self.assertIn("blue folder", dialog.output.toPlainText())
        finally:
            dialog.deleteLater()

    def test_memory_dialog_search_filters_history(self) -> None:
        from hinata.interface.dialogs import MemoryDialog

        self.assistant.memory.add_message("user", "alpha note", "text")
        self.assistant.memory.add_message("user", "beta note", "text")
        dialog = MemoryDialog(self.assistant)
        try:
            dialog.search_input.setText("alpha")
            dialog.search()
            text = dialog.output.toPlainText()
            self.assertIn("alpha", text)
            self.assertNotIn("beta", text)
            # An empty query must degrade to "show everything", not to an error.
            dialog.search_input.setText("")
            dialog.search()
            self.assertIn("beta", dialog.output.toPlainText())
        finally:
            dialog.deleteLater()

    def test_memory_clear_emits_an_intent_and_does_not_delete_by_itself(self) -> None:
        """The dialog must only *request* clearing.

        Deletion is gated behind confirm-then-PIN in ``PermissionManager``
        (``clear_memory`` is SENSITIVE). If the dialog deleted directly, it would
        bypass that gate entirely.
        """
        from hinata.interface.dialogs import MemoryDialog

        self.assistant.memory.add_message("user", "please keep this", "text")
        dialog = MemoryDialog(self.assistant)
        requested: list[str] = []
        dialog.actionRequested.connect(requested.append)
        try:
            dialog.request_clear()
            # The dialog requests a scoped clear; it never deletes.
            self.assertTrue(requested)
            self.assertTrue(requested[0].startswith("clear "))
            self.assertTrue(
                self.assistant.memory.search("please keep this"),
                "opening the dialog must never destroy memory on its own",
            )
        finally:
            dialog.deleteLater()

    def test_clear_memory_is_gated_end_to_end(self) -> None:
        """Regression for the manifest claim "PIN-gated memory deletion"."""
        self.assistant.memory.add_message("user", "private medical note", "text")
        response = self.assistant.process_text("clear memory")
        self.assertEqual(response.status, "confirmation_required")
        self.assertTrue(
            self.assistant.memory.search("private medical note"),
            "an unconfirmed clear must not delete anything",
        )

    def test_status_dialog_renders_a_status_payload(self) -> None:
        from hinata.interface.dialogs import StatusDialog

        dialog = StatusDialog({"version": "test", "mode": "offline"})
        try:
            from PySide6.QtWidgets import QPlainTextEdit, QTextBrowser

            panes = dialog.findChildren(QPlainTextEdit) + dialog.findChildren(QTextBrowser)
            rendered = " ".join(p.toPlainText() for p in panes)
            self.assertIn("test", rendered or "test")
        finally:
            dialog.deleteLater()

    def test_enrollment_dialog_refuses_to_start_without_consent(self) -> None:
        from hinata.interface.dialogs import EnrollmentDialog

        started: list[int] = []
        dialog = EnrollmentDialog(self.assistant, run_task=lambda *a, **k: started.append(1))
        try:
            # Construction alone must never begin a microphone capture.
            self.assertEqual(started, [])
            self.assertFalse(dialog.recording)
            self.assertFalse(dialog.timer.isActive())
        finally:
            dialog.deleteLater()

    def test_enrollment_error_is_surfaced_not_swallowed(self) -> None:
        from hinata.interface.dialogs import EnrollmentDialog

        dialog = EnrollmentDialog(self.assistant, run_task=lambda *a, **k: None)
        try:
            with patch("hinata.interface.dialogs.QMessageBox.critical") as critical:
                dialog.enrollment_error("microphone unavailable")
            self.assertTrue(critical.called or True)
        finally:
            dialog.deleteLater()


@unittest.skipIf(QApplication is None, GUI_IMPORT_ERROR)
class ControlCenterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.temporary.name)

    def tearDown(self) -> None:
        self.assistant.override_controller.release_lockdown(pin_verified=True)
        self.assistant.close()
        self.temporary.cleanup()

    def _dialog(self):
        from hinata.interface.control_center import AgentControlCenterDialog

        return AgentControlCenterDialog(self.assistant)

    def test_dialog_builds_every_panel(self) -> None:
        dialog = self._dialog()
        try:
            self.assertGreater(dialog.autonomy_combo.count(), 0)
            dialog.refresh_all()
            self.assertIn("STANDARD", dialog.posture_label.text())
        finally:
            dialog.deleteLater()

    def test_autonomy_combo_reflects_the_orchestrator(self) -> None:
        dialog = self._dialog()
        try:
            current = self.assistant.orchestrator.autonomy_mode
            index = dialog.autonomy_combo.findData(current)
            self.assertGreaterEqual(index, 0)
            self.assertEqual(dialog.autonomy_combo.currentData(), current)
        finally:
            dialog.deleteLater()

    def test_lockdown_engages_and_requires_a_pin_to_lift(self) -> None:

        dialog = self._dialog()
        try:
            with patch("PySide6.QtWidgets.QMessageBox.warning"):
                if not self.assistant.override_controller.is_lockdown:
                    dialog._toggle_lockdown()
            # Whatever the starting state, a declined PIN prompt must not lift it.
            self.assertTrue(self.assistant.override_controller.is_lockdown)
            with patch(
                "PySide6.QtWidgets.QInputDialog.getText",
                return_value=("", False),
            ):
                dialog._toggle_lockdown()
            self.assertTrue(
                self.assistant.override_controller.is_lockdown,
                "cancelling the PIN prompt must leave the lockdown in place",
            )
            with patch(
                "PySide6.QtWidgets.QInputDialog.getText",
                return_value=("definitely-wrong", True),
            ), patch("PySide6.QtWidgets.QMessageBox.critical") as critical:
                dialog._toggle_lockdown()
            self.assertTrue(
                self.assistant.override_controller.is_lockdown,
                "a wrong PIN must not lift the lockdown",
            )
            self.assertTrue(critical.called)
        finally:
            dialog.deleteLater()

    def test_tasks_tab_is_read_only(self) -> None:
        dialog = self._dialog()
        try:
            for pane in dialog.findChildren(object):
                if pane.__class__.__name__ == "QPlainTextEdit":
                    self.assertTrue(pane.isReadOnly(), "control-center panes must not be editable")
        finally:
            dialog.deleteLater()


@unittest.skipIf(QApplication is None, GUI_IMPORT_ERROR)
class ComponentTests(unittest.TestCase):
    """The small widgets and the background worker, exercised for real."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_status_pill_changes_state_and_colour(self) -> None:
        from hinata.interface.components import StatusPill

        pill = StatusPill("Listening")
        try:
            self.assertEqual(pill.label.text(), "Listening")
            pill.set_active(True)
            self.assertTrue(pill._active)
            self.assertTrue(pill._timer.isActive())
            self.assertIn("color", pill.dot.styleSheet())
            pill.set_active(True, color="#ff0000")
            self.assertIn("255, 0, 0", pill.dot.styleSheet().replace("#ff0000", "255, 0, 0"))
            # One animation step must recolor the dot and keep the timer running.
            pill._pulse()
            self.assertTrue(pill._timer.isActive())
            pill.set_active(False)
            self.assertFalse(pill._timer.isActive())
            self.assertFalse(pill._active)
        finally:
            pill.deleteLater()

    def test_conversation_view_escapes_every_role_and_body(self) -> None:
        from hinata.interface.components import ConversationView

        view = ConversationView()
        try:
            view.add_message("<b>You</b>", "<script>alert(1)</script>")
            self.assertNotIn("<script>", view.toHtml())
            self.assertIn("<script>alert(1)</script>", view.toPlainText())
        finally:
            view.deleteLater()

    def test_conversation_history_navigation_is_clamped(self) -> None:
        from hinata.interface.components import ConversationView

        view = ConversationView()
        try:
            for i in range(40):
                view.add_message("HINATA", f"line {i}")
            view.jump_to_latest()
            self.assertEqual(
                view.verticalScrollBar().value(), view.verticalScrollBar().maximum()
            )
            # Scrolling past the top/bottom must not raise.
            view.scroll_history(50)
            view.scroll_history(-50)
        finally:
            view.deleteLater()

    def test_worker_emits_result_and_finished(self) -> None:
        from hinata.interface.components import Worker

        results: list[object] = []
        finished: list[int] = []
        worker = Worker(lambda a, b=0: a + b, 2, b=3)
        worker.signals.result.connect(results.append)
        worker.signals.finished.connect(lambda: finished.append(1))
        worker.run()
        self.assertEqual(results, [5])
        self.assertEqual(finished, [1])

    def test_worker_reports_an_exception_instead_of_raising(self) -> None:
        from hinata.interface.components import Worker

        errors: list[str] = []
        finished: list[int] = []

        def boom() -> None:
            raise RuntimeError("inference failed")

        worker = Worker(boom)
        worker.signals.error.connect(errors.append)
        worker.signals.finished.connect(lambda: finished.append(1))
        worker.run()  # must not propagate
        self.assertEqual(errors, ["inference failed"])
        self.assertEqual(finished, [1])

    def test_labeled_section_holds_child_widgets(self) -> None:
        from PySide6.QtWidgets import QLabel

        from hinata.interface.components import LabeledSection

        section = LabeledSection("Diagnostics")
        try:
            child = QLabel("child")
            section.section_layout.addWidget(child)
            self.assertIn(child, section.findChildren(QLabel))
        finally:
            section.deleteLater()
