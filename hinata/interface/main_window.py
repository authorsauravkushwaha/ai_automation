"""Responsive PySide6 desktop front end for the shared HINATA pipeline."""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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

from hinata import __version__
from hinata.core.assistant import HinataAssistant
from hinata.core.types import AssistantResponse
from hinata.interface.canvas import OrbLogo, SceneCanvas
from hinata.interface.components import ConversationView, StatusPill, Worker
from hinata.interface.control_center import AgentControlCenterDialog
from hinata.interface.dialogs import EnrollmentDialog, MemoryDialog, SettingsDialog, StatusDialog
from hinata.interface.styles import STYLE_SHEET
from hinata.speech.microphone import HandsFreeSettings
from hinata.speech.replies import speaking_enabled, spoken_reply
from hinata.speech.speech_to_text import NoSpeechDetected
from hinata.speech.voice_pipeline import VoiceResult


class SpeechSignals(QObject):
    state = Signal(str, object)


class MainWindow(QMainWindow):
    def __init__(self, assistant: HinataAssistant) -> None:
        super().__init__()
        self.assistant = assistant
        self.workers: set[Worker] = set()
        self.pending_tasks = 0
        self._stop_generation = 0
        self._voice_session = False
        self._voice_capturing = False
        self._voice_resume_at = 0.0
        self._voice_timer = QTimer(self)
        self._voice_timer.setInterval(100)
        self._voice_timer.timeout.connect(self._voice_tick)
        self.last_response_text = ""
        self._browser_retry_query: str | None = None
        self.speech_signals = SpeechSignals()
        self.speech_signals.state.connect(self.update_speech_state)
        self.threadpool = QThreadPool(self)
        self.threadpool.setMaxThreadCount(4)
        self.threadpool.setExpiryTimeout(30_000)
        self._load_ui_settings()
        self.setWindowTitle(f"HINATA {__version__} — Local AI Assistant")
        self.setMinimumSize(720, 560)
        self.resize(920, 700)
        self._build_ui()

    def change_personality_mode(self, index: int) -> None:
        mode = self.mode_combo.itemData(index)
        if mode:
            selected = self.assistant.personality.set_mode(str(mode))
            self.statusBar().showMessage(f"HINATA is now in {selected.label} mode", 4000)

    def _load_ui_settings(self) -> None:
        self.speak_responses = speaking_enabled(self.assistant.config.interface, self.assistant.database)
        llm = self.assistant.database.get_setting("ui.llm_enabled")
        if llm is not None:
            self.assistant.orchestrator.llm_enabled = llm == "true"
        microphone = self.assistant.database.get_setting("ui.microphone")
        if microphone is not None:
            self.assistant.microphone.device = (
                int(microphone) if microphone.isascii() and microphone.isdigit() else microphone or None
            )
        self.assistant.microphone.hands_free = HandsFreeSettings.from_saved(
            self.assistant.database.get_setting("ui.hands_free_pause_ms"),
            self.assistant.database.get_setting("ui.hands_free_sensitivity"),
        )
        owner_name = self.assistant.database.get_setting("owner.name")
        if owner_name:
            self.assistant.personality.set_owner_name(owner_name)
        web_enabled = self.assistant.database.get_setting("ui.web_search_enabled")
        if web_enabled is not None:
            enabled = web_enabled == "true"
            self.assistant.config.security.web_search_enabled = enabled
            self.assistant.orchestrator.web_search.enabled = enabled
        voice_gender = self.assistant.database.get_setting("ui.voice_gender")
        if voice_gender in {"female", "male", "any"}:
            self.assistant.config.speech.tts_voice_gender = voice_gender

    def _build_ui(self) -> None:
        # The animated 3D backdrop paints itself; every panel floats on top.
        central = SceneCanvas()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)

        header = QFrame()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.addWidget(OrbLogo(header))
        title_layout = QVBoxLayout()
        title = QLabel("HINATA")
        title.setObjectName("brand")
        subtitle = QLabel("YOUR PRIVATE DESKTOP ASSISTANT")
        subtitle.setObjectName("subtitle")
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        header_layout.addLayout(title_layout)
        header_layout.addStretch()
        self.connection_badge = QLabel("●  CONNECTING TO OLLAMA…")
        self.connection_badge.setObjectName("localBadge")
        self.connection_badge.setToolTip(
            "Local Ollama connection. HINATA starts it automatically and never uses a cloud model."
        )
        header_layout.addWidget(self.connection_badge)
        self.mode_combo = QComboBox()
        for mode in self.assistant.personality.choices:
            self.mode_combo.addItem(f"{mode.label} mode", mode.key)
        current_index = self.mode_combo.findData(self.assistant.personality.mode.key)
        self.mode_combo.setCurrentIndex(current_index)
        self.mode_combo.setToolTip("Choose HINATA's conversational style; security permissions stay unchanged")
        self.mode_combo.currentIndexChanged.connect(self.change_personality_mode)
        header_layout.addWidget(self.mode_combo)
        self.reconnect_button = QPushButton("Reconnect")
        self.reconnect_button.setToolTip("Start the local Ollama service and check the local model")
        self.reconnect_button.clicked.connect(self.reconnect_ollama)
        header_layout.addWidget(self.reconnect_button)
        control_button = QPushButton("Agent center")
        control_button.setToolTip("Inspect tasks, plan graphs, specialist agents, workspaces, RAG, and security")
        control_button.clicked.connect(self.open_control_center)
        status_button = QPushButton("System status")
        status_button.clicked.connect(self.open_status)
        settings_button = QPushButton("Settings")
        settings_button.clicked.connect(self.open_settings)
        header_layout.addWidget(control_button)
        header_layout.addWidget(status_button)
        header_layout.addWidget(settings_button)
        outer.addWidget(header)

        self.conversation = ConversationView()
        outer.addWidget(self.conversation, 1)
        self.conversation.add_message(
            "HINATA",
            self.assistant.personality.welcome_greeting()
            + " Type a request or start hands-free voice. Structured PC commands work even when Ollama is offline.",
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
        latest = QPushButton("Latest message")
        latest.setToolTip("Jump to the latest chat message")
        latest.clicked.connect(self.conversation.jump_to_latest)
        indicators.addWidget(latest)
        outer.addLayout(indicators)

        input_row = QHBoxLayout()
        self.microphone_button = QPushButton("Start hands-free voice")
        self.microphone_button.setToolTip("Listen locally until you stop speaking; click again to pause hands-free voice")
        self.microphone_button.clicked.connect(self.toggle_microphone)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask HINATA or enter a command…")
        self.input.returnPressed.connect(self.submit_text)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self.submit_text)
        self.browser_retry_button = QPushButton("Try in browser")
        self.browser_retry_button.setToolTip("Send the unsuccessful web query to your default browser")
        self.browser_retry_button.clicked.connect(self.retry_in_browser)
        self.browser_retry_button.hide()
        input_row.addWidget(self.microphone_button)
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.send_button)
        input_row.addWidget(self.browser_retry_button)
        outer.addLayout(input_row)

        tools = QHBoxLayout()
        briefing_button = QPushButton("Tactical briefing")
        briefing_button.setToolTip("Synthesize live hardware, queue, daemon, and security briefing")
        briefing_button.clicked.connect(lambda: self.submit_text("executive briefing"))
        speak_button = QPushButton("Speak last response")
        speak_button.clicked.connect(lambda: self.speak_text(self.last_response_text))
        voice_check = QPushButton("Test speaker")
        voice_check.setToolTip("Play a short offline test and show any audio error")
        voice_check.clicked.connect(lambda: self.speak_text(self.assistant.personality.voice_test_phrase()))
        self.stop_button = QPushButton("Emergency stop")
        self.stop_button.setToolTip("Stop speech, microphone capture, wake listening, and request task cancellation")
        self.stop_button.clicked.connect(self.emergency_stop)
        lockdown_button = QPushButton("Red Alert Lockdown")
        lockdown_button.setToolTip("Immediately freeze all mutating and autonomous tools until PIN unlock")
        lockdown_button.clicked.connect(self.quick_lockdown)
        enroll_button = QPushButton("Voice enrollment")
        enroll_button.clicked.connect(self.open_enrollment)
        memory_button = QPushButton("Memory")
        memory_button.clicked.connect(self.open_memory)
        tools.addWidget(briefing_button)
        tools.addWidget(speak_button)
        tools.addWidget(voice_check)
        tools.addWidget(self.stop_button)
        tools.addWidget(lockdown_button)
        tools.addStretch()
        tools.addWidget(enroll_button)
        tools.addWidget(memory_button)
        outer.addLayout(tools)

        footer = QHBoxLayout()
        self.model_label = QLabel(
            f"Model: {self.assistant.config.ollama.model or 'auto-detect'}  •  CPU profile  •  "
            f"STT: {self.assistant.config.speech.stt_backend}"
        )
        self.model_label.setObjectName("muted")
        footer.addWidget(self.model_label)
        footer.addStretch()
        privacy = QLabel("No telemetry  •  No cloud conversations")
        privacy.setObjectName("muted")
        footer.addWidget(privacy)
        outer.addLayout(footer)
        self.statusBar().showMessage("Local memory ready")
        self.input.setFocus()
        # Connect to the local Ollama runtime as soon as the window is ready, so a
        # fresh install chats without the owner starting a service by hand.
        if self.assistant.orchestrator.llm_enabled:
            if self.assistant.config.ollama.auto_pull:
                QTimer.singleShot(150, lambda: self.connect_ollama(download_missing=True))
            else:
                QTimer.singleShot(150, self.connect_ollama)
        else:
            self.connection_badge.setText("●  LOCAL MODEL OFF")

    def run_task(
        self,
        function: Callable[..., Any],
        on_result: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
        label: str = "Processing",
    ) -> None:
        generation = self._stop_generation

        def run_if_current() -> Any:
            if generation != self._stop_generation:
                return None
            return function()

        def deliver_if_current(result: Any) -> None:
            # In particular, never turn a late voice transcript into a new command
            # after the owner has pressed the emergency stop button.
            if generation == self._stop_generation:
                on_result(result)

        def deliver_error_if_current(message: str) -> None:
            if generation == self._stop_generation:
                (on_error or self.show_task_error)(message)

        self._pause_voice_capture()
        worker = Worker(run_if_current)
        self.workers.add(worker)
        self.pending_tasks += 1
        self.activity_label.setText(label)
        self._update_busy_state()
        queued = Qt.ConnectionType.QueuedConnection
        worker.signals.result.connect(deliver_if_current, queued)
        worker.signals.error.connect(deliver_error_if_current, queued)
        worker.signals.finished.connect(lambda worker=worker: self.finish_task(worker), queued)
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
        # Keep the composer active while earlier requests run; thread-pool workers
        # allow independent tasks to overlap (model requests remain serialized safely).
        self.send_button.setEnabled(True)
        self.input.setEnabled(True)
        self.microphone_button.setEnabled(self._voice_session or not busy)

    def show_task_error(self, message: str) -> None:
        self.conversation.add_message("System", f"Error: {message}")
        self.statusBar().showMessage("Task failed; no success was assumed", 7000)
        if self.speak_responses:
            self.speak_text("I could not complete that request. Please check the error on screen.")

    def submit_text(self, text: str | None = None) -> None:
        value = text if isinstance(text, str) else self.input.text()
        cleaned = value.strip()
        if not cleaned:
            return
        self.input.clear()
        if not self._navigate_chat(cleaned):
            self._send_text(cleaned)

    def _navigate_chat(self, text: str) -> bool:
        """Handle local chat navigation without adding messages or invoking a model."""
        command = " ".join(text.casefold().strip().rstrip(".!?").split())
        command = re.sub(r"^hinata(?:[,!:]\s*|\s+)(?:please\s+)?|^please\s+", "", command)
        if command == "scroll chat up":
            self.conversation.scroll_history(-1)
        elif command == "scroll chat down":
            self.conversation.scroll_history(1)
        elif command in {"latest message", "go to latest message", "go to the latest message"}:
            self.conversation.jump_to_latest()
        else:
            return False
        return True

    def _send_text(self, cleaned: str) -> None:
        self._set_browser_retry(None)
        self.conversation.add_message("You", cleaned)
        self.run_task(
            lambda: self.assistant.process_text(cleaned),
            self.handle_response,
            label="Understanding request",
        )

    def _set_browser_retry(self, query: str | None) -> None:
        self._browser_retry_query = query
        self.browser_retry_button.setVisible(query is not None)

    def retry_in_browser(self) -> None:
        if self._browser_retry_query:
            # Keep any draft in the input box. Browser-first syntax treats the
            # whole query as data, and dispatch rechecks current permissions.
            query = " ".join(self._browser_retry_query.split())
            self._send_text(f"search in browser for {query}")

    def handle_response(self, response: AssistantResponse, voice: VoiceResult | None = None) -> None:
        self.conversation.add_message("HINATA", response.text)
        retry = response.data.get("browser_retry_query")
        web = self.assistant.orchestrator.web_search
        self._set_browser_retry(retry if isinstance(retry, str) and retry and web.enabled and not web.strict_offline else None)
        if self.speak_responses:
            self.speak_text(spoken_reply(response))
        self.statusBar().showMessage(response.status.replace("_", " "), 5000)
        if response.status == "model_unavailable":
            text = response.text.lower()
            connected_but_wrong = "unexpected format" in text
            if not connected_but_wrong:
                self.connection_badge.setText("●  OLLAMA OFFLINE")
            # Keep the header honest and make the one-click fix obvious.
            self.reconnect_button.setEnabled(True)
            self.reconnect_button.setObjectName("primary")
            self._repolish(self.reconnect_button)
        source = "voice" if voice is not None else "text"
        speaker_match = voice.speaker_match if voice is not None else None
        speaker_required = voice.speaker_required if voice is not None else False
        if response.status == "verification_required" and response.data.get("task_id"):
            task_id = response.data["task_id"]
            try:
                task, checks = self.assistant.review_task(task_id)
            except (ValueError, RuntimeError) as exc:
                self.show_task_error(str(exc))
                return
            details = "\n".join(f"Step {check['step']} ({check['tool']}): {check['status']} — {check['detail']}" for check in checks)
            request = task.get("user_request") or "Not recorded; inspect the task history manually."
            answer = QMessageBox.question(
                self, "Review task outcome",
                f"Original request: {request}\nPlan objective: {task['objective']}\n\n{details}\n\n"
                "Have you personally checked that the original request was satisfied? "
                "Choose No to leave the task awaiting review.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.conversation.add_message("System", f"Task {task_id} remains unverified. Review it later with hinata tasks.")
                return
            evidence, accepted = QInputDialog.getMultiLineText(
                self, "Record observed evidence", "Describe what you personally checked (10–1000 characters):"
            )
            if not accepted:
                self.conversation.add_message("System", f"Task {task_id} remains unverified.")
                return
            try:
                if self.assistant.attest_task(task_id, evidence, satisfied=True):
                    self.conversation.add_message("System", "Human review recorded; task marked complete.")
                else:
                    self.conversation.add_message("System", "Task state changed; review was not recorded.")
            except (ValueError, RuntimeError) as exc:
                self.show_task_error(str(exc))
            return
        if response.needs_plan_confirmation:
            answer = QMessageBox.question(
                self,
                "Approve HINATA plan",
                f"{response.text}\n\nApprove this entire plan? No step has run yet.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                if response.data.get("task_id"):
                    self.assistant.tasks.cancel_pending(response.data["task_id"])
                self.conversation.add_message("System", "Plan cancelled; no steps were executed.")
                if self.speak_responses:
                    self.speak_text("Plan cancelled. No steps were executed.")
                return
            self.run_task(
                lambda: self.assistant.execute_plan(
                    response.plan_actions,
                    task_id=response.data.get("task_id"),
                    source=source,
                    speaker_match=speaker_match,
                    speaker_required=speaker_required,
                ),
                lambda result: self.handle_response(result, voice),
                label="Executing approved plan",
            )
            return
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
                if self.speak_responses:
                    self.speak_text("Action cancelled. Nothing was executed.")
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
                if self.speak_responses:
                    self.speak_text("Action cancelled. Nothing was executed.")
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

    def toggle_microphone(self) -> None:
        if self._voice_session:
            self._end_voice_session()
            if not self.pending_tasks:
                self.activity_label.setText("Ready")
            return
        if self.pending_tasks or self.assistant.microphone.recording:
            return
        self._voice_session = True
        self.microphone_button.setText("Pause voice")
        self._voice_timer.start()
        self._voice_tick()

    def _pause_voice_capture(self) -> None:
        if self._voice_capturing:
            self._voice_capturing = False
            self.assistant.microphone.cancel()
            self.listening_pill.set_active(False)

    def _end_voice_session(self) -> None:
        self._voice_session = False
        self._voice_timer.stop()
        self._pause_voice_capture()
        self.microphone_button.setText("Start hands-free voice")
        self._update_busy_state()

    def _voice_tick(self) -> None:
        if not self._voice_session:
            return
        if self.assistant.tts.speaking:
            # Leave a short buffer for audio playback to drain before opening the mic.
            self._voice_resume_at = time.monotonic() + 0.3
        if self.pending_tasks or QApplication.activeModalWidget() or time.monotonic() < self._voice_resume_at:
            self._pause_voice_capture()
            return
        if self.assistant.tts.speaking:
            return
        try:
            if not self._voice_capturing:
                if self.assistant.microphone.recording:
                    return
                self.assistant.voice.start()
                self._voice_capturing = True
                self.listening_pill.set_active(True, "#ef7f89")
                self.activity_label.setText("Listening — speak, then pause to send")
            elif self.assistant.microphone.poll_utterance():
                # Stop and transcribe in the worker. Don't let run_task discard this turn.
                self._voice_capturing = False
                self.listening_pill.set_active(False)
                self.run_task(
                    self._finish_utterance, self.process_voice_result, self.voice_error,
                    "Recognizing speech locally",
                )
        except RuntimeError as exc:
            self.voice_error(str(exc))

    def _finish_utterance(self) -> VoiceResult | None:
        try:
            return self.assistant.voice.stop_and_process()
        except NoSpeechDetected:
            return None

    def process_voice_result(self, voice: VoiceResult | None) -> None:
        if voice is None:
            return
        voice_authorized = voice.speaker_match is True or (voice.speaker_match is None and not voice.speaker_required)
        if voice_authorized and self._navigate_chat(voice.text):
            return
        self.conversation.add_message("You", f"[Voice: {voice.speaker_status}] {voice.text}")
        if voice.text.strip().rstrip(".!?").casefold() == "pause voice":
            self._end_voice_session()
            self.conversation.add_message("HINATA", "Voice paused. Select Start hands-free voice to resume.")
            return
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
        self._end_voice_session()
        self.activity_label.setText("Voice paused")
        self.show_task_error(message)

    def speak_text(self, text: str) -> None:
        if text:
            self._pause_voice_capture()
            self._voice_resume_at = time.monotonic() + 0.3
            try:
                self.assistant.tts.speak(text, self.speech_signals.state.emit)
            except (RuntimeError, OSError) as exc:
                self.statusBar().showMessage(f"Speech output error: {exc}", 8000)

    def update_speech_state(self, state: str, error: object) -> None:
        self.speaking_pill.set_active(state == "speaking", "#78bcef")
        if state == "error" and error:
            self.statusBar().showMessage(f"Speech output error: {error}", 8000)

    def emergency_stop(self) -> None:
        self._stop_generation += 1
        self._set_browser_retry(None)
        self._end_voice_session()
        self.assistant.emergency_stop()
        self.speaking_pill.set_active(False)
        self.activity_label.setText("Stop requested — an action already in progress may finish")
        self.statusBar().showMessage("Stop requested; HINATA will not start another action", 7000)

    # --------------------------------------------------------------- Ollama
    def connect_ollama(self, *, download_missing: bool = False) -> None:
        """Start/verify loopback Ollama in the background and report the result."""
        self.connection_badge.setText("●  CONNECTING TO OLLAMA…")
        self.reconnect_button.setEnabled(False)
        label = (
            "Downloading the local model (one-time internet)"
            if download_missing
            else "Connecting to local Ollama"
        )
        self.run_task(
            lambda: self.assistant.orchestrator.recheck_model(download_missing=download_missing),
            self.apply_connection_state,
            self.connection_error,
            label,
        )

    def connection_error(self, message: str) -> None:
        self.reconnect_button.setEnabled(True)
        self.connection_badge.setText("●  OLLAMA OFFLINE")
        self.statusBar().showMessage(f"Local model unavailable: {message}", 9000)
        self.show_task_error(message)

    def apply_connection_state(self, report: dict[str, Any]) -> None:
        self.reconnect_button.setEnabled(True)
        if report.get("state") in {"ready", "model_missing"}:
            # Connected: return the button to its quiet default style.
            self.reconnect_button.setObjectName("")
            self._repolish(self.reconnect_button)
        state = str(report.get("state", ""))
        if state == "ready":
            model = str(report.get("model") or self.assistant.config.ollama.model)
            self.connection_badge.setText("●  LOCAL ONLY")
            self.model_label.setText(
                f"Model: {model}  •  CPU profile  •  STT: {self.assistant.config.speech.stt_backend}"
            )
            self.statusBar().showMessage("Local Ollama connected", 4000)
            return
        if state == "model_missing":
            self.connection_badge.setText("●  MODEL MISSING")
            self.statusBar().showMessage(
                "Ollama is running but the local model is not downloaded", 8000
            )
            self.offer_model_download(str(report.get("model") or ""))
            return
        if state == "not_installed":
            self.connection_badge.setText("●  OLLAMA NOT INSTALLED")
            self.conversation.add_message(
                "System",
                str(report.get("guidance") or "Ollama is not installed.")
                + "\nStructured commands such as 'open notepad' and 'system info' still work.",
            )
            return
        if state == "disabled":
            self.connection_badge.setText("●  LOCAL MODEL OFF")
            return
        self.connection_badge.setText("●  OLLAMA OFFLINE")
        detail = report.get("error") or report.get("guidance") or "Ollama did not answer."
        self.conversation.add_message(
            "System",
            f"{detail}\nStructured commands such as 'open notepad', 'system info', and "
            "'find files for report in Downloads' still work without the model. "
            "Press Reconnect to try starting Ollama again.",
        )

    def offer_model_download(self, model: str) -> None:
        name = model or self.assistant.config.ollama.model or "the configured model"
        answer = QMessageBox.question(
            self,
            "Download the local model",
            f"Ollama is connected, but {name} is not downloaded yet.\n\n"
            "Download it now? This is the only step that uses internet; afterwards all "
            "responses are generated on this PC.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.connect_ollama(download_missing=True)

    def reconnect_ollama(self) -> None:
        if self.pending_tasks:
            return
        self.connect_ollama()

    def open_settings(self) -> None:
        self._pause_voice_capture()
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
        self.assistant.personality.set_owner_name(dialog.owner_name_input.text())
        web_enabled = dialog.web_search_checkbox.isChecked()
        self.assistant.config.security.web_search_enabled = web_enabled
        self.assistant.orchestrator.web_search.enabled = web_enabled
        if not web_enabled:
            self._set_browser_retry(None)
        voice_gender = str(dialog.voice_gender_combo.currentData())
        self.assistant.config.speech.tts_voice_gender = voice_gender
        selected = dialog.selected_microphone
        self.assistant.microphone.device = (
            int(selected) if selected.isascii() and selected.isdigit() else selected or None
        )
        self.assistant.database.set_setting("ui.speak_responses", str(self.speak_responses).lower())
        self.assistant.database.set_setting(
            "ui.llm_enabled", str(self.assistant.orchestrator.llm_enabled).lower()
        )
        hands_free = dialog.hands_free_settings
        self.assistant.microphone.hands_free = hands_free
        self.assistant.database.set_setting("ui.hands_free_pause_ms", str(hands_free.pause_ms))
        self.assistant.database.set_setting("ui.hands_free_sensitivity", hands_free.sensitivity)
        self.assistant.database.set_setting("ui.microphone", selected)
        self.assistant.database.set_setting("ui.web_search_enabled", str(web_enabled).lower())
        self.assistant.database.set_setting("ui.voice_gender", voice_gender)
        if self.assistant.orchestrator.llm_enabled and not self.pending_tasks:
            QTimer.singleShot(0, self.connect_ollama)
        self.statusBar().showMessage("Settings saved locally", 4000)

    def open_memory(self) -> None:
        dialog = MemoryDialog(self.assistant, self)
        dialog.actionRequested.connect(self.submit_text)
        dialog.exec()

    def open_control_center(self) -> None:
        AgentControlCenterDialog(self.assistant, self).exec()

    def quick_lockdown(self) -> None:
        if not self.assistant.override_controller.is_lockdown:
            self._end_voice_session()
            self.assistant.engage_lockdown("Main Window Red Alert Lockdown")
            self.conversation.add_message(
                "System",
                "RED ALERT LOCKDOWN ENGAGED. All mutating, UI, and autonomous tools are frozen until unlocked with your Owner PIN.",
            )
            self.statusBar().showMessage("RED ALERT LOCKDOWN ACTIVE", 6000)
            return
        pin, ok = QInputDialog.getText(
            self, "Lift Red Alert Lockdown", "Enter HINATA Owner PIN:", QLineEdit.EchoMode.Password
        )
        if not ok or not pin:
            return
        try:
            self.assistant.release_lockdown(pin)
            self.conversation.add_message("System", "Red Alert Lockdown lifted. Normal guarded operations restored.")
            self.statusBar().showMessage("Lockdown lifted", 4000)
        except PermissionError as exc:
            QMessageBox.critical(self, "PIN Verification Failed", str(exc))

    def open_enrollment(self) -> None:
        if self.pending_tasks:
            QMessageBox.information(self, "Busy", "Finish the current task before enrollment.")
            return
        self._end_voice_session()
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
                self, "Task in progress", "Please wait for the active local task to finish before closing HINATA."
            )
            event.ignore()
            return
        self._end_voice_session()
        if self.assistant.microphone.recording:
            self.assistant.microphone.cancel()
        self.assistant.tts.stop()
        event.accept()

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def run_gui(assistant: HinataAssistant) -> int:
    instance = QApplication.instance()
    application = instance if isinstance(instance, QApplication) else QApplication(sys.argv)
    application.setApplicationName("HINATA")
    application.setOrganizationName("HINATA Local")
    application.setStyleSheet(STYLE_SHEET)
    window = MainWindow(assistant)
    window.show()
    if window.speak_responses:
        window.speak_text(assistant.personality.welcome_greeting())
    return int(application.exec())
