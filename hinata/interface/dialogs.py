"""Desktop settings, local-memory, and secure voice-enrollment dialogs."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from hinata.core.assistant import HinataAssistant


class SettingsDialog(QDialog):
    def __init__(
        self,
        assistant: HinataAssistant,
        *,
        speak_responses: bool,
        llm_enabled: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.assistant = assistant
        self.setWindowTitle("HINATA Settings")
        self.resize(600, 450)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        general = QWidget()
        form = QFormLayout(general)
        self.speak_checkbox = QCheckBox("Speak final responses using the offline engine")
        self.speak_checkbox.setChecked(speak_responses)
        self.llm_checkbox = QCheckBox("Use local Ollama for requests outside structured commands")
        self.llm_checkbox.setChecked(llm_enabled)
        form.addRow(self.speak_checkbox)
        form.addRow(self.llm_checkbox)
        form.addRow("Configured model", QLabel(assistant.config.ollama.model or "auto-detect"))
        form.addRow("Ollama endpoint", QLabel(assistant.config.ollama.host))
        connection = assistant.llm.connect_status()
        form.addRow(
            "Ollama connection",
            QLabel(self.describe_connection(str(connection.get("state", "")))),
        )
        form.addRow("Auto-start service", QLabel("Yes" if assistant.config.ollama.auto_start else "No"))
        form.addRow("Model changes", QLabel("Edit config.toml and restart HINATA"))
        tabs.addTab(general, "General")

        voice = QWidget()
        voice_layout = QVBoxLayout(voice)
        voice_form = QFormLayout()
        self.microphone_combo = QComboBox()
        self.microphone_combo.addItem("System default microphone", "")
        current_device = assistant.microphone.device
        voice_form.addRow("Microphone", self.microphone_combo)
        self.device_status = QLabel("Refresh to enumerate local input devices.")
        self.device_status.setWordWrap(True)
        self.device_status.setObjectName("muted")
        refresh = QPushButton("Refresh microphones")
        refresh.clicked.connect(self.refresh_devices)
        voice_form.addRow(refresh)
        voice_form.addRow("Speech-to-text", QLabel(assistant.config.speech.stt_backend))
        voice_form.addRow("Text-to-speech", QLabel(assistant.config.speech.tts_backend))
        voice_form.addRow(
            "Speaker profile",
            QLabel("Enrolled" if assistant.speaker.is_enrolled else "Not enrolled"),
        )
        voice_layout.addLayout(voice_form)
        voice_layout.addWidget(self.device_status)
        voice_layout.addStretch()
        tabs.addTab(voice, "Voice")

        privacy = QWidget()
        privacy_form = QFormLayout(privacy)
        privacy_form.addRow("Local data", QLabel(str(assistant.config.data_dir)))
        privacy_form.addRow("Voice embeddings", QLabel("DPAPI-protected on Windows; no raw samples saved"))
        privacy_form.addRow("Network policy", QLabel("Ollama loopback only; no cloud AI or telemetry"))
        roots = QLabel("\n".join(str(path) for path in assistant.config.security.allowed_roots))
        roots.setWordWrap(True)
        privacy_form.addRow("Permitted folders", roots)
        tabs.addTab(privacy, "Privacy")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._current_device = current_device
        QTimer.singleShot(0, self.refresh_devices)

    @staticmethod
    def describe_connection(state: str) -> str:
        return {
            "ready": "Connected, local model installed",
            "model_missing": "Connected, model not downloaded yet",
            "not_installed": "Ollama is not installed",
            "unreachable": "Not reachable; HINATA will start it on the next request",
            "disabled": "Local model disabled in config.toml",
        }.get(state, "Unknown")

    def refresh_devices(self) -> None:
        configured = "" if self._current_device is None else str(self._current_device)
        self.microphone_combo.clear()
        self.microphone_combo.addItem("System default microphone", "")
        try:
            devices = self.assistant.microphone.list_devices()
        except RuntimeError as exc:
            self.device_status.setText(str(exc))
        else:
            for device in devices:
                self.microphone_combo.addItem(
                    f"{device['index']}: {device['name']} ({device['input_channels']} ch)",
                    str(device["index"]),
                )
            self.device_status.setText(f"Found {len(devices)} input device(s). Selection is saved locally.")
        index = self.microphone_combo.findData(configured)
        if index >= 0:
            self.microphone_combo.setCurrentIndex(index)

    @property
    def selected_microphone(self) -> str:
        return str(self.microphone_combo.currentData())


class MemoryDialog(QDialog):
    actionRequested = Signal(str)

    def __init__(self, assistant: HinataAssistant, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.assistant = assistant
        self.setWindowTitle("Local Memory Controls")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Inspect or export stored history here. Settings and PIN material are excluded from exports."
        )
        explanation.setObjectName("muted")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search conversations and approved preferences…")
        self.search_input.returnPressed.connect(self.search)
        search_button = QPushButton("Search")
        search_button.clicked.connect(self.search)
        recent_button = QPushButton("Recent")
        recent_button.clicked.connect(self.refresh)
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(search_button)
        search_row.addWidget(recent_button)
        layout.addLayout(search_row)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)
        controls = QHBoxLayout()
        export_button = QPushButton("Export JSON…")
        export_button.clicked.connect(self.export)
        self.scope_combo = QComboBox()
        self.scope_combo.addItems(["conversations", "commands", "preferences", "all"])
        clear_button = QPushButton("Request deletion…")
        clear_button.setObjectName("danger")
        clear_button.clicked.connect(self.request_clear)
        controls.addWidget(export_button)
        controls.addStretch()
        controls.addWidget(QLabel("Delete scope"))
        controls.addWidget(self.scope_combo)
        controls.addWidget(clear_button)
        layout.addLayout(controls)
        close_button = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_button.rejected.connect(self.reject)
        layout.addWidget(close_button)
        self.refresh()

    def refresh(self) -> None:
        rows = self.assistant.memory.recent_messages(100)
        self.output.setPlainText(
            "\n\n".join(f"{row['created_at']}  {row['role']}\n{row['content']}" for row in rows)
            or "No conversation history is stored."
        )

    def search(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            self.refresh()
            return
        rows = self.assistant.memory.search(query, 50)
        self.output.setPlainText(
            "\n\n".join(f"{row['created_at']}  {row['kind']}\n{row['text']}" for row in rows)
            or "No matching local memory was found."
        )

    def export(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "Export local memory", "hinata-memory.json", "JSON (*.json)")
        if filename:
            destination = self.assistant.database.export_json(Path(filename))
            QMessageBox.information(self, "Export complete", f"Saved without settings or PIN material:\n{destination}")

    def request_clear(self) -> None:
        scope = self.scope_combo.currentText()
        self.actionRequested.emit("clear memory" if scope == "all" else f"clear {scope}")
        self.accept()


class EnrollmentDialog(QDialog):
    def __init__(
        self,
        assistant: HinataAssistant,
        run_task: Callable[..., None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.assistant = assistant
        self.run_task = run_task
        self.samples: list[Any] = []
        self.target_samples = 0
        self.recording = False
        self.processing = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.capture_sample)
        self.setWindowTitle("Owner Voice Enrollment")
        self.resize(530, 360)
        layout = QVBoxLayout(self)
        warning = QLabel(
            "Voice matching is not authentication. Enroll in a quiet room with your normal speaking voice. "
            "Raw samples stay in memory and are discarded after embeddings are created."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        form = QFormLayout()
        self.pin_input = QLineEdit()
        self.pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_input.setPlaceholderText("Required to create or replace a biometric profile")
        form.addRow("HINATA PIN", self.pin_input)
        self.count_input = QSpinBox()
        self.count_input.setRange(3, 8)
        self.count_input.setValue(3)
        form.addRow("Voice samples", self.count_input)
        self.duration_input = QDoubleSpinBox()
        self.duration_input.setRange(2.0, 15.0)
        self.duration_input.setValue(4.0)
        self.duration_input.setSuffix(" seconds each")
        form.addRow("Sample duration", self.duration_input)
        layout.addLayout(form)
        self.status = QLabel("Enter your PIN and start when you are ready.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.record_button = QPushButton("Start enrollment")
        self.record_button.setObjectName("primary")
        self.record_button.clicked.connect(self.start_sample)
        layout.addWidget(self.record_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.cancel_button)

    def start_sample(self) -> None:
        if self.recording:
            return
        if not self.assistant.authentication.is_configured:
            QMessageBox.warning(self, "PIN required", "Run 'hinata setup-pin' before voice enrollment.")
            return
        if not self.samples:
            if not self.assistant.authentication.verify_pin(self.pin_input.text()):
                QMessageBox.warning(self, "Verification failed", "The PIN is incorrect. No profile was changed.")
                return
            self.pin_input.clear()
            if self.assistant.speaker.is_enrolled:
                answer = QMessageBox.question(self, "Replace profile", "Replace the existing owner voice profile?")
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.target_samples = self.count_input.value()
        try:
            self.assistant.voice.start()
        except RuntimeError as exc:
            QMessageBox.critical(self, "Microphone error", str(exc))
            return
        self.recording = True
        self.record_button.setEnabled(False)
        self.status.setText(f"Recording sample {len(self.samples) + 1}/{self.target_samples}… speak naturally.")
        self.timer.start(int(self.duration_input.value() * 1000))

    def capture_sample(self) -> None:
        try:
            audio = self.assistant.microphone.stop()
        except RuntimeError as exc:
            self.recording = False
            self.record_button.setEnabled(True)
            QMessageBox.critical(self, "Microphone error", str(exc))
            return
        self.recording = False
        self.samples.append(audio)
        if len(self.samples) < self.target_samples:
            self.record_button.setEnabled(True)
            self.record_button.setText(f"Record sample {len(self.samples) + 1}")
            self.status.setText("Sample captured in memory. Record the next phrase with slightly different wording.")
            return
        self.status.setText("Creating protected local embeddings; raw recordings will now be discarded…")
        self.processing = True
        self.cancel_button.setEnabled(False)
        self.run_task(
            lambda samples=list(self.samples): self.assistant.enroll_owner(samples),
            self.enrollment_complete,
            self.enrollment_error,
            "Enrolling voice",
        )

    def enrollment_complete(self, result: dict[str, Any]) -> None:
        self.processing = False
        self.samples.clear()
        QMessageBox.information(
            self,
            "Enrollment complete",
            f"Enrolled {result['samples']} voice embeddings. No raw audio was saved.\n"
            "Uncertain matches will be rejected; sensitive actions still require your PIN.",
        )
        self.accept()

    def enrollment_error(self, message: str) -> None:
        self.processing = False
        self.cancel_button.setEnabled(True)
        self.samples.clear()
        self.record_button.setEnabled(True)
        self.record_button.setText("Start enrollment again")
        self.status.setText("Enrollment failed. No raw samples were saved.")
        QMessageBox.critical(self, "Enrollment failed", message)

    def reject(self) -> None:
        if self.processing:
            QMessageBox.information(self, "Enrollment in progress", "Wait for local embedding creation to finish.")
            return
        self.timer.stop()
        if self.assistant.microphone.recording:
            self.assistant.microphone.cancel()
        self.samples.clear()
        self.pin_input.clear()
        super().reject()


class StatusDialog(QDialog):
    def __init__(self, status: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("HINATA System & Model Status")
        self.resize(620, 520)
        layout = QVBoxLayout(self)
        output = QPlainTextEdit()
        output.setReadOnly(True)
        output.setPlainText(json.dumps(status, indent=2))
        layout.addWidget(output)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
