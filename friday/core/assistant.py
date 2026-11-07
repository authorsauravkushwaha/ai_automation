"""Composition root shared by terminal and PySide6 front ends."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from friday.automation.app_control import AppControl
from friday.automation.file_manager import FileManager
from friday.automation.system_info import SystemInfo
from friday.automation.windows_control import WindowsControl
from friday.config import FridayConfig, load_config
from friday.core.intent_parser import IntentParser
from friday.core.llm_manager import LLMManager
from friday.core.memory import Memory
from friday.core.orchestrator import Orchestrator
from friday.core.permissions import PermissionManager
from friday.core.types import ActionRequest, AssistantResponse
from friday.database.database import Database
from friday.security.action_validator import ActionValidator
from friday.security.authentication import Authentication
from friday.speech.microphone import Microphone
from friday.speech.speaker_recognition import SpeakerRecognition
from friday.speech.speech_to_text import SpeechToText
from friday.speech.text_to_speech import TextToSpeech
from friday.speech.voice_pipeline import VoicePipeline, VoiceResult
from friday.speech.wake_word import WakeWordDetector


class FridayAssistant:
    def __init__(self, config: FridayConfig | None = None, *, llm_enabled: bool | None = None) -> None:
        self.config = config or load_config()
        self.config.ensure_private_directories()
        self._configure_logging()
        self.database = Database(self.config.database_path)
        self.memory = Memory(self.database)
        self.authentication = Authentication(self.database)
        self.validator = ActionValidator()
        self.parser = IntentParser(self.validator)
        self.llm = LLMManager(self.config.ollama)
        self.permissions = PermissionManager(self.authentication)
        self.orchestrator = Orchestrator(
            validator=self.validator,
            parser=self.parser,
            llm=self.llm,
            memory=self.memory,
            permissions=self.permissions,
            apps=AppControl(self.config.applications),
            files=FileManager(self.config.security),
            windows=WindowsControl(),
            system=SystemInfo(),
            llm_enabled=self.config.ollama.enabled if llm_enabled is None else llm_enabled,
        )
        self.microphone = Microphone(self.config.speech.microphone, self.config.speech.sample_rate)
        self.stt = SpeechToText(self.config.speech)
        self.speaker = SpeakerRecognition(self.config.speech, self.config.speaker_directory)
        self.voice = VoicePipeline(self.microphone, self.stt, self.speaker)
        self.tts = TextToSpeech(self.config.speech)
        self.wake_word = WakeWordDetector(self.config.wake_word)

    def _configure_logging(self) -> None:
        root = logging.getLogger("friday")
        if any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
            return
        root.setLevel(logging.INFO)
        handler = RotatingFileHandler(
            self.config.log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
        root.propagate = False

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

    def status(self, *, check_model: bool = True) -> dict[str, Any]:
        import importlib.util

        return {
            "version": __import__("friday").__version__,
            "data_directory": str(self.config.data_dir),
            "database": str(self.config.database_path),
            "log": str(self.config.log_path),
            "allowed_roots": [str(path) for path in self.config.security.allowed_roots],
            "pin_configured": self.authentication.is_configured,
            "speaker_enrolled": self.speaker.is_enrolled,
            "speech_backend": self.config.speech.stt_backend,
            "tts_backend": self.config.speech.tts_backend,
            "optional_packages": {
                name: importlib.util.find_spec(name) is not None
                for name in ("PySide6", "faster_whisper", "vosk", "sounddevice", "pyttsx3", "resemblyzer", "openwakeword")
            },
            "model": self.llm.status() if check_model else {"model": self.config.ollama.model},
            "wake_word_enabled": self.config.wake_word.enabled,
        }

    def close(self) -> None:
        if self.microphone.recording:
            self.microphone.cancel()
        self.tts.stop()
        self.stt.unload()
        self.speaker.unload()
        self.wake_word.close()

    @property
    def log_path(self) -> Path:
        return self.config.log_path
