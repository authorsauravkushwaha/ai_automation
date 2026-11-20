"""Composition root shared by terminal and PySide6 front ends."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from hinata.agents.registry import AgentRegistry
from hinata.automation.app_control import AppControl
from hinata.automation.file_manager import FileManager
from hinata.automation.jarvis_cockpit import DesktopCockpit, JarvisOverrideController
from hinata.automation.system_info import SystemInfo
from hinata.automation.web_search import WebSearchEngine
from hinata.automation.windows_control import WindowsControl
from hinata.automation.workspace import WorkspaceManager
from hinata.config import HinataConfig, load_config
from hinata.core.assets import OfflineAssetManager
from hinata.core.errors import TaskBudget
from hinata.core.intent_parser import IntentParser
from hinata.core.llm_manager import LLMManager
from hinata.core.memory import Memory
from hinata.core.observability import TraceRecorder
from hinata.core.ollama_service import OllamaService
from hinata.core.orchestrator import Orchestrator
from hinata.core.permissions import PermissionManager
from hinata.core.personality import PersonalityManager
from hinata.core.planner import Planner
from hinata.core.providers import HardwareCapabilityService
from hinata.core.tool_registry import ToolRegistry
from hinata.core.types import ActionRequest, AssistantResponse
from hinata.database.database import Database
from hinata.knowledge.embeddings import OllamaEmbeddings
from hinata.knowledge.index import KnowledgeIndex, ThrottledKnowledgeWatcher
from hinata.security.action_validator import ActionValidator
from hinata.security.authentication import Authentication
from hinata.security.local_protection import LocalVault
from hinata.security.offline_guard import OfflineGuard
from hinata.speech.microphone import Microphone
from hinata.speech.speaker_recognition import SpeakerRecognition
from hinata.speech.speech_to_text import SpeechToText
from hinata.speech.text_to_speech import TextToSpeech
from hinata.speech.voice_pipeline import VoicePipeline, VoiceResult
from hinata.speech.wake_word import WakeWordDetector, WakeWordService
from hinata.tasks.playbooks import PlaybookManager
from hinata.tasks.scheduler import TaskScheduler
from hinata.tasks.store import TaskStateError, TaskStore


class HinataAssistant:
    def __init__(self, config: HinataConfig | None = None, *, llm_enabled: bool | None = None) -> None:
        self.config = config or load_config()
        self._log_handler: RotatingFileHandler | None = None
        self.config.ensure_private_directories()
        self._configure_logging()
        self.vault = (
            LocalVault(self.config.data_dir, enabled=True)
            if self.config.security.encrypt_at_rest
            else None
        )
        self.offline_guard = OfflineGuard(
            strict_offline=self.config.security.strict_offline,
            online_mode_enabled=self.config.security.online_mode_enabled,
            allowed_online_domains=self.config.security.allowed_online_domains,
        )
        if self.config.security.strict_offline:
            self.offline_guard.install_socket_guard()

        self.database = Database(
            self.config.database_path,
            vault=self.vault,
            encryption_backend=self.config.security.database_encryption_backend,
        )
        self.embeddings = OllamaEmbeddings(
            LLMManager(self.config.ollama, OllamaService(self.config.ollama)),
            self.config.ollama.embedding_model,
        )
        self.ollama = OllamaService(self.config.ollama)
        self.llm = LLMManager(self.config.ollama, self.ollama)
        self.embeddings = OllamaEmbeddings(self.llm, self.config.ollama.embedding_model)
        self.memory = Memory(
            self.database,
            self.config.memory.conversation_limit,
            embeddings=self.embeddings if self.config.ollama.enabled else None,
        )
        self.personality = PersonalityManager(
            self.database, self.config.personality.mode,
            default_user_name=self.config.personality.user_name,
        )
        saved_voice_gender = self.database.get_setting("ui.voice_gender")
        if saved_voice_gender in {"female", "male", "any"}:
            self.config.speech.tts_voice_gender = saved_voice_gender
        saved_web_search = self.database.get_setting("ui.web_search_enabled")
        if saved_web_search is not None:
            self.config.security.web_search_enabled = saved_web_search == "true"
        self.authentication = Authentication(self.database)
        self.validator = ActionValidator()
        self.parser = IntentParser(self.validator)
        self.planner = Planner(self.validator)
        self.override_controller = JarvisOverrideController()
        self.permissions = PermissionManager(
            self.authentication, override_controller=self.override_controller
        )
        self.files = FileManager(self.config.security)
        self.cockpit = DesktopCockpit(self.files)
        self.knowledge = KnowledgeIndex(self.database, self.files, self.embeddings)
        self.knowledge_watcher = ThrottledKnowledgeWatcher(self.knowledge)
        self.tasks = TaskStore(self.database, max_revisions=max(1, self.config.agentic.max_replans))
        self.playbooks = PlaybookManager(self.database, self.planner, self.tasks)
        self.scheduler = TaskScheduler(self.database)
        self.workspaces = WorkspaceManager(self.database, self.config.security.allowed_roots)
        self.tool_registry = ToolRegistry(self.database)
        self.agent_registry = AgentRegistry(
            self.database,
            max_delegation_depth=self.config.agentic.max_delegation_depth,
            max_parallel_readonly=self.config.agentic.max_parallel_readonly,
        )
        self.tracer = TraceRecorder(self.database)
        self.assets = OfflineAssetManager(self.database, self.config)
        self.hardware = HardwareCapabilityService()

        budget = TaskBudget(
            max_iterations=self.config.agentic.max_iterations,
            max_steps=self.config.agentic.max_plan_steps,
            max_duration_seconds=self.config.agentic.max_duration_seconds,
            max_model_calls=self.config.agentic.max_model_calls,
            max_tool_calls=self.config.agentic.max_tool_calls,
            max_replans=self.config.agentic.max_replans,
            per_tool_timeout_seconds=self.config.agentic.per_tool_timeout_seconds,
        )
        self.orchestrator = Orchestrator(
            validator=self.validator,
            parser=self.parser,
            planner=self.planner,
            llm=self.llm,
            memory=self.memory,
            personality=self.personality,
            permissions=self.permissions,
            apps=AppControl(
                self.config.applications,
                discover_installed=self.config.security.discover_installed_apps,
            ),
            files=self.files,
            web_search=WebSearchEngine(
                enabled=self.config.security.web_search_enabled,
                strict_offline=self.config.security.strict_offline,
                offline_guard=self.offline_guard,
            ),
            knowledge=self.knowledge,
            tasks=self.tasks,
            workspace_manager=self.workspaces,
            tool_registry=self.tool_registry,
            agent_registry=self.agent_registry,
            tracer=self.tracer,
            cockpit=self.cockpit,
            autonomy_mode=self.config.agentic.autonomy_mode,
            default_budget=budget,
            quarantine_dir=self.config.data_dir / "quarantine",
            online_mode_enabled=self.config.security.online_mode_enabled,
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
        from hinata.speech.multimodal import LocalVisionAdapter, VoiceDiagnostics
        self.voice_diagnostics = VoiceDiagnostics(
            self.config.speech, self.microphone, self.stt, self.tts, self.speaker
        )
        self.vision = LocalVisionAdapter(self.files, self.llm)

    def _configure_logging(self) -> None:
        root = logging.getLogger("hinata")
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

    def process_text(self, text: str, *, autonomy_mode: str | None = None) -> AssistantResponse:
        return self.orchestrator.process(text, source="text", autonomy_mode=autonomy_mode)

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

    def resume_task(self, task_id: str) -> AssistantResponse:
        """Continue only the verified remainder of the exact, still-approved plan."""
        task = self.tasks.get(task_id)
        if task is None:
            return AssistantResponse("Task not found.", False, "invalid")
        try:
            plan = self.tasks.restore_approved_plan(task_id, self.planner)
        except (TaskStateError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return AssistantResponse(f"Task cannot be resumed safely: {exc}", False, "invalid")
        if not self.tasks.resume_paused_task(task_id, plan):
            return AssistantResponse(
                "Task cannot be resumed: approval may be expired or a step has an uncertain/non-retryable outcome.",
                False,
                "blocked",
                data={"task_id": task_id},
            )
        return self.orchestrator.execute_plan(
            list(plan.steps),
            source=str(task["source"]),
            task_id=task_id,
            plan_override=plan,
            resume_existing=True,
        )

    def reconcile_task(self, task_id: str) -> dict[str, Any]:
        return self.tasks.reconcile_interrupted_task(
            task_id, reconciler=self.orchestrator.reviewer.reconcile_single_effect
        )

    def review_task(self, task_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        task = self.tasks.get(task_id)
        if not task or task["state"] != "VERIFYING":
            raise ValueError("Task is not awaiting verification")
        candidate = {
            "type": "plan",
            "objective": task["objective"],
            "steps": [
                {"tool": step["tool"], "arguments": json.loads(step["arguments_json"])}
                for step in task["steps"]
            ],
        }
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

    def engage_lockdown(self, reason: str = "Owner engaged Red Alert Lockdown") -> dict[str, Any]:
        """Immediately halt active operations and engage Red Alert Lockdown."""
        self.emergency_stop()
        state = self.override_controller.engage_lockdown(reason)
        self.tracer.record_event(
            run_id="tactical-override",
            agent_id="security_policy",
            event_type="lockdown_engaged",
            status="LOCKED_DOWN",
            payload={"reason": reason[:200]},
        )
        return state

    def release_lockdown(self, pin: str) -> dict[str, Any]:
        if not self.authentication.is_configured or not self.authentication.verify_pin(pin):
            raise PermissionError("Invalid or unconfigured owner PIN")
        state = self.override_controller.release_lockdown(pin_verified=True)
        self.tracer.record_event(
            run_id="tactical-override",
            agent_id="security_policy",
            event_type="lockdown_released",
            status="OK",
        )
        return state

    def activate_commander_mode(self, pin: str, *, minutes: int = 10, label: str = "Commander Session") -> dict[str, Any]:
        if not self.authentication.is_configured or not self.authentication.verify_pin(pin):
            raise PermissionError("Invalid or unconfigured owner PIN")
        state = self.override_controller.activate_commander_lease(
            pin_verified=True, minutes=minutes, label=label
        )
        self.tracer.record_event(
            run_id="tactical-override",
            agent_id="security_policy",
            event_type="commander_lease_activated",
            status="ACTIVE",
            payload={"minutes": minutes, "label": label[:120]},
        )
        return state

    def stand_down_commander_mode(self) -> dict[str, Any]:
        state = self.override_controller.stand_down()
        self.tracer.record_event(
            run_id="tactical-override",
            agent_id="security_policy",
            event_type="commander_lease_revoked",
            status="STANDARD",
        )
        return state

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

        from hinata.core.jarvis_models import select_optimal_fleet_pack
        from hinata.core.local_image import is_model_ready, model_directory
        from hinata.speech.replies import speaking_enabled
        from hinata.speech.text_to_speech import output_readiness

        hardware = self.hardware.detect()
        model_status = self.llm.status() if check_model else {"model": self.config.ollama.model}
        return {
            "version": __import__("hinata").__version__,
            "schema_version": self.database.schema_version,
            "data_directory": str(self.config.data_dir),
            "database": str(self.config.database_path),
            "log": str(self.config.log_path),
            "allowed_roots": [str(path) for path in self.config.security.allowed_roots],
            "autonomy_mode": self.orchestrator.autonomy_mode,
            "tactical_override": self.override_controller.status(),
            "offline_policy": self.offline_guard.status(),
            "web_search": {
                "enabled": self.config.security.web_search_enabled,
                "strict_offline": self.config.security.strict_offline,
                "behavior": (
                    "blocked"
                    if self.config.security.strict_offline or not self.config.security.web_search_enabled
                    else "try_live_sources_then_use_local_fallback"
                ),
                "sources": ["DuckDuckGo", "Wikipedia", "GitHub", "OpenLibrary (when relevant)"],
            },
            "encryption_at_rest": self.vault is not None,
            "database_encryption": self.database.encryption_status(),
            "knowledge_index": self.knowledge.index_stats(),
            "audit_chain_intact": self.tracer.verify_integrity()["intact"],
            "pin_configured": self.authentication.is_configured,
            "speaker_enrolled": self.speaker.is_enrolled,
            "personality_mode": self.personality.mode.key,
            "speech_backend": self.config.speech.stt_backend,
            "tts_backend": self.config.speech.tts_backend,
            "voice_output": {
                **output_readiness(self.config.speech),
                "replies_enabled": speaking_enabled(self.config.interface, self.database),
                "last_error": self.tts.last_error,
            },
            "optional_packages": {
                name: importlib.util.find_spec(name) is not None
                for name in (
                    "PySide6",
                    "pypdf",
                    "numpy",
                    "faster_whisper",
                    "vosk",
                    "sounddevice",
                    "pyttsx3",
                    "resemblyzer",
                    "openwakeword",
                    "sqlcipher3",
                    "hnswlib",
                )
            },
            "model": model_status,
            "ollama_connection": self.llm.connect_status(),
            "hardware_profile": hardware,
            "recommended_ai_fleet": select_optimal_fleet_pack(hardware).to_dict(),
            "local_image_model_ready": is_model_ready(model_directory(self.config.data_dir)),
            "configured_model_roles": {
                "chat": self.config.ollama.model,
                "planner": self.config.ollama.planner_model,
                "coder": self.config.ollama.coding_model,
                "reviewer": self.config.ollama.reviewer_model,
                "vision": self.config.ollama.vision_model,
                "embedding": self.config.ollama.embedding_model,
            },
            "agents": self.agent_registry.health_snapshot(),
            "metrics": self.tracer.metrics_snapshot(),
            "wake_word_enabled": self.config.wake_word.enabled,
        }

    def close(self) -> None:
        self.knowledge_watcher.stop()
        self.offline_guard.uninstall_socket_guard()
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
