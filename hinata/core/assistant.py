"""Composition root shared by terminal and PySide6 front ends."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AppControl
from hinata.automation.file_manager import FileManager
from hinata.automation.system_info import SystemInfo
from hinata.automation.windows_control import WindowsControl
from hinata.config import HinataConfig, load_config
from hinata.core.intent_parser import IntentParser
from hinata.core.llm_manager import LLMManager
from hinata.core.memory import Memory
from hinata.core.ollama_service import OllamaService
from hinata.core.orchestrator import Orchestrator
from hinata.core.permissions import PermissionManager
from hinata.core.personality import PersonalityManager
from hinata.core.planner import Planner
from hinata.core.types import ActionRequest, AssistantResponse
from hinata.database.database import Database
from hinata.knowledge.embeddings import OllamaEmbeddings
from hinata.knowledge.index import KnowledgeIndex
from hinata.security.action_validator import ActionValidator
from hinata.security.authentication import Authentication
from hinata.speech.microphone import Microphone
from hinata.speech.speaker_recognition import SpeakerRecognition
from hinata.speech.speech_to_text import SpeechToText
from hinata.speech.text_to_speech import TextToSpeech
from hinata.speech.voice_pipeline import VoicePipeline, VoiceResult
from hinata.speech.wake_word import WakeWordDetector, WakeWordService
from hinata.tasks.store import TaskStore


class HinataAssistant:
    def __init__(self, config: HinataConfig | None = None, *, llm_enabled: bool | None = None) -> None:
        self.config = config or load_config()
        self._log_handler: RotatingFileHandler | None = None
        self.config.ensure_private_directories()
        self._configure_logging()
        self.database = Database(self.config.database_path)
        self.memory = Memory(self.database, self.config.memory.conversation_limit)
        self.personality = PersonalityManager(self.database, self.config.personality.mode)
        self.authentication = Authentication(self.database)
        self.validator = ActionValidator()
        self.parser = IntentParser(self.validator)
        self.planner = Planner(self.validator)
        self.ollama = OllamaService(self.config.ollama)
        self.llm = LLMManager(self.config.ollama, self.ollama)
        self.permissions = PermissionManager(self.authentication)
        files = FileManager(self.config.security)
        self.knowledge = KnowledgeIndex(self.database, files, OllamaEmbeddings(self.llm, self.config.ollama.embedding_model))
        self.tasks = TaskStore(self.database)
        self.orchestrator = Orchestrator(
            validator=self.validator,
            parser=self.parser,
            planner=self.planner,
            llm=self.llm,
            memory=self.memory,
            personality=self.personality,
            permissions=self.permissions,
            apps=AppControl(self.config.applications),
            files=files,
            knowledge=self.knowledge,
            tasks=self.tasks,
            windows=WindowsControl(),
            system=SystemInfo(),
            llm_enabled=self.config.ollama.enabled if llm_enabled is None else llm_enabled,
            pull_missing_model=self.config.ollama.auto_pull,
        )
        self.microphone = Microphone(self.config.speech.microphone, self.config.speech.sample_rate)
        self.stt = SpeechToText(self.config.speech)
        self.speaker = SpeakerRecognition(self.config.speech, self.config.speaker_directory)
        self.voice = VoicePipeline(self.microphone, self.stt, self.speaker)
        self.tts = TextToSpeech(self.config.speech)
        self.wake_word = WakeWordDetector(self.config.wake_word)
        self.wake_service = WakeWordService(self.wake_word, self.config.speech.microphone)

    def _configure_logging(self) -> None:
        root = logging.getLogger("hinata")
        # HINATA has one composition root per process. Close a prior instance's
        # handler so Windows can move/delete its private data directory cleanly.
        for existing in list(root.handlers):
            if isinstance(existing, RotatingFileHandler):
                root.removeHandler(existing)
                existing.close()
        root.setLevel(logging.INFO)
        handler = RotatingFileHandler(
            self.config.log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.propagate = False
        self._log_handler = handler

    def process_text(self, text: str) -> AssistantResponse:
        return self.orchestrator.process(text, source="text")

    def process_voice(self, duration_seconds: float = 5.0) -> tuple[VoiceResult, AssistantResponse]:
        voice = self.voice.listen(duration_seconds)
        response = self.orchestrator.process(
            voice.text,
            source="voice",
            speaker_match=voice.speaker_match,
            speaker_required=voice.speaker_required,
        )
        return voice, response

    def execute_plan(
        self,
        actions: list[ActionRequest],
        *,
        source: str = "text",
        speaker_match: bool | None = None,
        speaker_required: bool = False,
        task_id: str | None = None,
    ) -> AssistantResponse:
        return self.orchestrator.execute_plan(
            actions,
            source=source,
            speaker_match=speaker_match,
            speaker_required=speaker_required,
            task_id=task_id,
        )

    def review_task(self, task_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        task = self.tasks.get(task_id)
        if not task or task["state"] != "VERIFYING":
            raise ValueError("Task is not awaiting verification")
        candidate = {"type": "plan", "objective": task["objective"], "steps": [
            {"tool": step["tool"], "arguments": json.loads(step["arguments_json"])}
            for step in task["steps"]
        ]}
        plan = self.planner.validate(candidate)
        return task, [asdict(check) for check in self.orchestrator.reviewer.review(plan.steps)]

    def attest_task(self, task_id: str, evidence: str, *, satisfied: bool) -> bool:
        _, checks = self.review_task(task_id)
        if satisfied and any(check["status"] == "contradicted" for check in checks):
            raise ValueError("A postcondition is contradicted; task cannot be marked complete")
        return self.tasks.attest(task_id, evidence, satisfied=satisfied, checks=checks)

    def resume(
        self,
        action: ActionRequest,
        *,
        source: str = "text",
        speaker_match: bool | None = None,
        speaker_required: bool = False,
        pin: str | None = None,
    ) -> AssistantResponse:
        return self.orchestrator.resume(
            action,
            source=source,
            speaker_match=speaker_match,
            speaker_required=speaker_required,
            confirmed=True,
            pin=pin,
        )

    def enroll_owner(self, samples: list[Any]) -> dict[str, Any]:
        return self.speaker.enroll(samples)

    def emergency_stop(self) -> None:
        """Stop speech/recording and request cooperative cancellation of the current action."""
        self.orchestrator.request_stop()
        if self.microphone.recording:
            self.microphone.cancel()
        self.tts.stop()
        self.wake_service.stop()
        self.wake_word.close()

    def status(self, *, check_model: bool = True) -> dict[str, Any]:
        import importlib.util

        return {
            "version": __import__("hinata").__version__,
            "data_directory": str(self.config.data_dir),
            "database": str(self.config.database_path),
            "log": str(self.config.log_path),
            "allowed_roots": [str(path) for path in self.config.security.allowed_roots],
            "pin_configured": self.authentication.is_configured,
            "speaker_enrolled": self.speaker.is_enrolled,
            "personality_mode": self.personality.mode.key,
            "speech_backend": self.config.speech.stt_backend,
            "tts_backend": self.config.speech.tts_backend,
            "optional_packages": {
                name: importlib.util.find_spec(name) is not None
                for name in ("PySide6", "faster_whisper", "vosk", "sounddevice", "pyttsx3", "resemblyzer", "openwakeword")
            },
            "model": self.llm.status() if check_model else {"model": self.config.ollama.model},
            "ollama_connection": self.llm.connect_status(),
            "wake_word_enabled": self.config.wake_word.enabled,
        }

    def close(self) -> None:
        if self.microphone.recording:
            self.microphone.cancel()
        self.tts.stop()
        self.stt.unload()
        self.speaker.unload()
        self.wake_service.stop()
        self.wake_word.close()
        if self._log_handler is not None:
            root = logging.getLogger("hinata")
            if self._log_handler in root.handlers:
                root.removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None

    @property
    def log_path(self) -> Path:
        return self.config.log_path
