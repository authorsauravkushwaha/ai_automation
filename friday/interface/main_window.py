"""Responsive PySide6 desktop front end for the shared FRIDAY pipeline."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from friday import __version__
from friday.core.assistant import FridayAssistant
from friday.core.types import AssistantResponse
from friday.interface.components import ConversationView, StatusPill, Worker
from friday.interface.dialogs import EnrollmentDialog, MemoryDialog, SettingsDialog, StatusDialog
from friday.interface.styles import STYLE_SHEET
from friday.speech.voice_pipeline import VoiceResult


class SpeechSignals(QObject):
    state = Signal(str, object)


class MainWindow(QMainWindow):
    def __init__(self, assistant: FridayAssistant) -> None:
        super().__init__()
        self.assistant = assistant
        self.workers: set[Worker] = set()
        self.pending_tasks = 0
        self.last_response_text = ""
        self.speech_signals = SpeechSignals()
        self.speech_signals.state.connect(self.update_speech_state)
        self.threadpool = QThreadPool(self)
        self.threadpool.setMaxThreadCount(2)
        self.threadpool.setExpiryTimeout(30_000)
        self._load_ui_settings()
        self.setWindowTitle(f"FRIDAY {__version__} — Local AI Assistant")
        self.setMinimumSize(720, 560)
        self.resize(920, 700)
        self._build_ui()

    def _load_ui_settings(self) -> None:
        speak = self.assistant.database.get_setting("ui.speak_responses")
        self.speak_responses = (
            speak == "true" if speak is not None else self.assistant.config.interface.speak_responses
        )
        llm = self.assistant.database.get_setting("ui.llm_enabled")
        if llm is not None:
            self.assistant.orchestrator.llm_enabled = llm == "true"
        microphone = self.assistant.database.get_setting("ui.microphone")
        if microphone is not None:
            self.assistant.microphone.device = (
                int(microphone) if microphone.isascii() and microphone.isdigit() else microphone or None
            )

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        title_layout = QVBoxLayout()
        title = QLabel("FRIDAY")
        title.setObjectName("brand")
        subtitle = QLabel("YOUR PRIVATE DESKTOP ASSISTANT")
        subtitle.setObjectName("subtitle")
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        header_layout.addLayout(title_layout)
        header_layout.addStretch()
        badge = QLabel("●  LOCAL ONLY")
        badge.setObjectName("localBadge")
        header_layout.addWidget(badge)
        status_button = QPushButton("System status")
        status_button.clicked.connect(self.open_status)
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self.open_settings)
        header_layout.addWidget(status_button)
        header_layout.addWidget(settings_button)
        outer.addWidget(header)

        self.conversation = ConversationView()
        outer.addWidget(self.conversation, 1)
        self.conversation.add_message(
            "FRIDAY",
            "Ready. Type a request or start voice input. Structured PC commands work even when Ollama is offline. "
            "Nothing is sent to a cloud AI service.",
        )

        indicators = QHBoxLayout()
        self.listening_pill = StatusPill("LISTENING")
        self.processing_pill = StatusPill("PROCESSING")
        self.speaking_pill = StatusPill("SPEAKING")
        indicators.addWidget(self.listening_pill)
        indicators.addWidget(self.processing_pill)
        indicators.addWidget(self.speaking_pill)
        indicators.addStretch()
        self.activity_label = QLabel("Ready")
        self.activity_label.setObjectName("muted")
        indicators.addWidget(self.activity_label)
        outer.addLayout(indicators)

        input_row = QHBoxLayout()
        self.microphone_button = QPushButton("Start voice")
        self.microphone_button.setToolTip("Start recording; click again to stop and transcribe locally")
        self.microphone_button.clicked.connect(self.toggle_microphone)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask FRIDAY or enter a command…")
        self.input.returnPressed.connect(self.submit_text)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.submit_text)
        input_row.addWidget(self.microphone_button)
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.send_button)
        outer.addLayout(input_row)

        tools = QHBoxLayout()
        speak_button = QPushButton("Speak last response")
        speak_button.clicked.connect(lambda: self.speak_text(self.last_response_text))
        self.stop_button = QPushButton("Stop audio")
        self.stop_button.clicked.connect(self.stop_audio)
        enroll_button = QPushButton("Voice enrollment")
        enroll_button.clicked.connect(self.open_enrollment)
        memory_button = QPushButton("Memory")
        memory_button.clicked.connect(self.open_memory)
        tools.addWidget(speak_button)
        tools.addWidget(self.stop_button)
        tools.addStretch()
        tools.addWidget(enroll_button)
        tools.addWidget(memory_button)
        outer.addLayout(tools)

        footer = QHBoxLayout()
        model = QLabel(
            f"Model: {self.assistant.config.ollama.model}  •  CPU profile  •  "
            f"STT: {self.assistant.config.speech.stt_backend}"
        )
        model.setObjectName("muted")
        footer.addWidget(model)
        footer.addStretch()
        privacy = QLabel("No telemetry  •  No cloud conversations")
        privacy.setObjectName("muted")
        footer.addWidget(privacy)
        outer.addLayout(footer)
        self.statusBar().showMessage("Local memory ready")
        self.input.setFocus()

    def run_task(
        self,
        function: Callable[..., Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
        label: str = "Processing",
    ) -> None:
        worker = Worker(function)
        self.workers.add(worker)
        self.pending_tasks += 1
        self.activity_label.setText(label)
        self._update_busy_state()
        worker.signals.result.connect(on_result)
        if on_error is not None:
            worker.signals.error.connect(on_error)
        else:
            worker.signals.error.connect(self.show_task_error)
        worker.signals.finished.connect(lambda worker=worker: self.finish_task(worker))
        self.threadpool.start(worker)

    def finish_task(self, worker: Worker) -> None:
        self.workers.discard(worker)
        self.pending_tasks = max(0, self.pending_tasks - 1)
        if not self.pending_tasks:
            self.activity_label.setText("Ready")
        self._update_busy_state()

    def _update_busy_state(self) -> None:
        busy = self.pending_tasks > 0
        self.processing_pill.set_active(busy, "#e7b66b")
        self.send_button.setEnabled(not busy)
        self.microphone_button.setEnabled(not busy and not self.assistant.microphone.recording)

    def show_task_error(self, message: str) -> None:
        self.conversation.add_message("System", f"Error: {message}")
        self.statusBar().showMessage("Task failed; no success was assumed", 7000)

    def submit_text(self, text: str | None = None) -> None:
        if self.pending_tasks:
            return
        value = text if isinstance(text, str) else self.input.text()
        cleaned = value.strip()
        if not cleaned:
            return
        self.input.clear()
        self.conversation.add_message("You", cleaned)
        self.run_task(
            lambda: self.assistant.process_text(cleaned),
            self.handle_response,
            label="Understanding request",
        )

    def handle_response(self, response: AssistantResponse, voice: VoiceResult | None = None) -> None:
        self.conversation.add_message("FRIDAY", response.text)
        self.statusBar().showMessage(response.status.replace("_", " "), 5000)
        source = "voice" if voice is not None else "text"
        speaker_match = voice.speaker_match if voice is not None else None
        speaker_required = voice.speaker_required if voice is not None else False
        if response.needs_confirmation and response.action is not None:
            action = response.action
            details = json.dumps(action.arguments, indent=2, ensure_ascii=False)
            answer = QMessageBox.question(
                self,
                "Approve controlled action",
                f"{response.text}\n\nTool: {action.tool}\nArguments:\n{details}\n\n"
                "No action has run yet. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.conversation.add_message("System", "Action cancelled; nothing was executed.")
                return
            self.run_task(
                lambda: self.assistant.resume(
                    action,
                    source=source,
                    speaker_match=speaker_match,
                    speaker_required=speaker_required,
                ),
                lambda result: self.handle_response(result, voice),
                label="Checking authorization",
            )
            return
        if response.needs_pin and response.action is not None:
            action = response.action
            pin, accepted = QInputDialog.getText(
                self,
                "Sensitive action authorization",
                response.text,
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                self.conversation.add_message("System", "Action cancelled; nothing was executed.")
                return
            # The worker consumes and removes the only task-held PIN copy.
            secret = [pin]

            def authorized() -> AssistantResponse:
                return self.assistant.resume(
                    action,
                    source=source,
                    speaker_match=speaker_match,
                    speaker_required=speaker_required,
                    pin=secret.pop(),
                )

            pin = ""
            self.run_task(
                authorized,
                lambda result: self.handle_response(result, voice),
                label="Verifying PIN locally",
            )
            return
        self.last_response_text = response.text
        if self.speak_responses:
            self.speak_text(response.text)

    def toggle_microphone(self) -> None:
        if self.assistant.microphone.recording:
            try:
                self.listening_pill.set_active(False)
                self.run_task(
                    self.assistant.voice.stop_and_process,
                    self.process_voice_result,
                    self.voice_error,
                    "Recognizing speech locally",
                )
            except RuntimeError as exc:
                self.show_task_error(str(exc))
            return
        if self.pending_tasks:
            return
        try:
            self.assistant.voice.start()
        except RuntimeError as exc:
            QMessageBox.critical(self, "Microphone", str(exc))
            return
        self.microphone_button.setEnabled(True)
        self.microphone_button.setText("Stop & process")
        self.microphone_button.setObjectName("recording")
        self._repolish(self.microphone_button)
        self.listening_pill.set_active(True, "#ef7f89")
        self.activity_label.setText("Listening — click Stop & process when finished")

    def process_voice_result(self, voice: VoiceResult) -> None:
        self.reset_microphone_button()
        self.conversation.add_message("You", f"[Voice: {voice.speaker_status}] {voice.text}")
        self.run_task(
            lambda: self.assistant.orchestrator.process(
                voice.text,
                source="voice",
                speaker_match=voice.speaker_match,
                speaker_required=voice.speaker_required,
            ),
            lambda response: self.handle_response(response, voice),
            label="Understanding voice request",
        )

    def voice_error(self, message: str) -> None:
        self.reset_microphone_button()
        self.listening_pill.set_active(False)
        self.show_task_error(message)

    def reset_microphone_button(self) -> None:
        self.microphone_button.setText("Start voice")
        self.microphone_button.setObjectName("")
        self._repolish(self.microphone_button)
        self.listening_pill.set_active(False)

    def speak_text(self, text: str) -> None:
        if text:
            self.assistant.tts.speak(text, self.speech_signals.state.emit)

    def update_speech_state(self, state: str, error: object) -> None:
        self.speaking_pill.set_active(state == "speaking", "#78bcef")
        if state == "error" and error:
            self.statusBar().showMessage(f"Speech output error: {error}", 8000)

    def stop_audio(self) -> None:
        self.assistant.tts.stop()
        if self.assistant.microphone.recording:
            self.assistant.microphone.cancel()
            self.reset_microphone_button()
        self.speaking_pill.set_active(False)
        self.activity_label.setText("Audio stopped")

    def open_settings(self) -> None:
        dialog = SettingsDialog(
            self.assistant,
            speak_responses=self.speak_responses,
            llm_enabled=self.assistant.orchestrator.llm_enabled,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.speak_responses = dialog.speak_checkbox.isChecked()
        self.assistant.orchestrator.llm_enabled = dialog.llm_checkbox.isChecked()
        selected = dialog.selected_microphone
        self.assistant.microphone.device = (
            int(selected) if selected.isascii() and selected.isdigit() else selected or None
        )
        self.assistant.database.set_setting("ui.speak_responses", str(self.speak_responses).lower())
        self.assistant.database.set_setting(
            "ui.llm_enabled", str(self.assistant.orchestrator.llm_enabled).lower()
        )
        self.assistant.database.set_setting("ui.microphone", selected)
        self.statusBar().showMessage("Settings saved locally", 4000)

    def open_memory(self) -> None:
        dialog = MemoryDialog(self.assistant, self)
        dialog.actionRequested.connect(self.submit_text)
        dialog.exec()

    def open_enrollment(self) -> None:
        if self.pending_tasks or self.assistant.microphone.recording:
            QMessageBox.information(self, "Busy", "Finish the current task before enrollment.")
            return
        EnrollmentDialog(self.assistant, self.run_task, self).exec()

    def open_status(self) -> None:
        self.run_task(
            self.assistant.status,
            self.show_status_dialog,
            label="Checking local runtime",
        )

    def show_status_dialog(self, status: dict[str, Any]) -> None:
        StatusDialog(status, self).exec()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.pending_tasks:
            QMessageBox.information(
                self, "Task in progress", "Please wait for the active local task to finish before closing FRIDAY."
            )
            event.ignore()
            return
        if self.assistant.microphone.recording:
            self.assistant.microphone.cancel()
        self.assistant.tts.stop()
        event.accept()

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def run_gui(assistant: FridayAssistant) -> int:
    instance = QApplication.instance()
    application = instance if isinstance(instance, QApplication) else QApplication(sys.argv)
    application.setApplicationName("FRIDAY")
    application.setOrganizationName("FRIDAY Local")
    application.setStyleSheet(STYLE_SHEET)
    window = MainWindow(assistant)
    window.show()
    return int(application.exec())
