"""Understand -> plan -> validate -> authorize -> execute -> verify -> report."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from typing import Any

from hinata.automation.app_control import AppControl, AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.system_info import SystemInfo
from hinata.automation.windows_control import WindowsControl
from hinata.core.intent_parser import IntentParser, PlanError
from hinata.core.llm_manager import LLMManager, OllamaError
from hinata.core.memory import Memory
from hinata.core.permissions import PermissionManager
from hinata.core.personality import PersonalityManager
from hinata.core.planner import ExecutionPlan, Planner, PlanningError
from hinata.core.types import ActionRequest, ActionResult, AssistantResponse
from hinata.security.action_validator import ActionValidator, ValidationError

LOGGER = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        *,
        validator: ActionValidator,
        parser: IntentParser,
        planner: Planner,
        llm: LLMManager,
        memory: Memory,
        personality: PersonalityManager,
        permissions: PermissionManager,
        apps: AppControl,
        files: FileManager,
        windows: WindowsControl,
        system: SystemInfo,
        llm_enabled: bool = True,
    ) -> None:
        self.validator = validator
        self.parser = parser
        self.planner = planner
        self.llm = llm
        self.memory = memory
        self.personality = personality
        self.permissions = permissions
        self.apps = apps
        self.files = files
        self.windows = windows
        self.system = system
        self.llm_enabled = llm_enabled
        self._stop_requested = threading.Event()

    def request_stop(self) -> None:
        """Request a cooperative stop and close an in-flight Ollama response."""
        self._stop_requested.set()
        self.llm.cancel_active_request()

    def _cancelled(self) -> bool:
        return self._stop_requested.is_set()

    @staticmethod
    def _cancel_response() -> AssistantResponse:
        return AssistantResponse("Stopped. Any action not already completed was not started.", False, "cancelled")

    def process(
        self,
        text: str,
        *,
        source: str = "text",
        speaker_match: bool | None = None,
        speaker_required: bool = False,
    ) -> AssistantResponse:
        self._stop_requested.clear()
        cleaned = text.strip()
        if not cleaned:
            return AssistantResponse("Please enter a request.", False, "clarification")
        if len(cleaned) > 4000:
            return AssistantResponse("Requests are limited to 4000 characters.", False, "invalid")
        self.memory.add_message("user", cleaned, source)
        try:
            action = self.parser.parse(cleaned)
        except ValidationError as exc:
            response = AssistantResponse(f"Invalid command: {exc}", False, "invalid")
            self.memory.add_message("assistant", response.text, source)
            return response

        if action is None and self.llm_enabled:
            try:
                raw_plan = self.llm.complete(
                    cleaned,
                    self.memory.recent_messages(),
                    self.memory.preferences(),
                    self.validator.tool_descriptions(),
                    self.personality.mode.instruction,
                )
                kind, payload = self.parser.parse_llm_plan(raw_plan, cleaned)
                if kind == "tool":
                    action = payload
                elif kind == "plan":
                    plan = self.planner.validate(payload)
                    response = AssistantResponse(
                        plan.preview(), False, "plan_confirmation_required",
                        plan_actions=list(plan.steps),
                    )
                    self.memory.add_message("assistant", response.text, source)
                    return response
                else:
                    response = AssistantResponse(
                        str(payload), True, "clarification" if kind == "clarify" else "completed"
                    )
                    self.memory.add_message("assistant", response.text, source)
                    return response
            except (OllamaError, PlanError, PlanningError) as exc:
                if self._cancelled():
                    response = self._cancel_response()
                    self.memory.add_message("assistant", response.text, source)
                    return response
                response = AssistantResponse(
                    f"I could not use the local model: {exc}\n"
                    "Structured commands such as 'open notepad', 'system info', and "
                    "'find files for report in Downloads' still work.",
                    False,
                    "model_unavailable",
                )
                self.memory.add_message("assistant", response.text, source)
                return response

        if self._cancelled():
            response = self._cancel_response()
            self.memory.add_message("assistant", response.text, source)
            return response

        if action is None:
            response = AssistantResponse(
                "I need a clearer request. Try 'open notepad', 'create folder Projects', "
                "'volume up', 'system info', or enable Ollama for natural-language chat.",
                True,
                "clarification",
            )
            self.memory.add_message("assistant", response.text, source)
            return response
        response = self._execute(
            action,
            source=source,
            speaker_match=speaker_match,
            speaker_required=speaker_required,
            confirmed=False,
            pin=None,
        )
        self.memory.add_message("assistant", response.text, source)
        return response

    def resume(
        self,
        action: ActionRequest,
        *,
        source: str = "text",
        speaker_match: bool | None = None,
        speaker_required: bool = False,
        confirmed: bool = True,
        pin: str | None = None,
    ) -> AssistantResponse:
        self._stop_requested.clear()
        response = self._execute(
            action,
            source=source,
            speaker_match=speaker_match,
            speaker_required=speaker_required,
            confirmed=confirmed,
            pin=pin,
        )
        self.memory.add_message("assistant", response.text, source)
        return response

    def execute_plan(
        self,
        actions: list[ActionRequest],
        *,
        source: str = "text",
        speaker_match: bool | None = None,
        speaker_required: bool = False,
    ) -> AssistantResponse:
        """Revalidate and execute only the owner-approved, low-impact plan subset."""
        self._stop_requested.clear()
        candidate = {
            "type": "plan",
            "objective": "Owner-approved task",
            "steps": [
                {"tool": action.tool, "arguments": action.arguments}
                for action in actions
            ],
        }
        try:
            plan = self.planner.validate(candidate)
        except PlanningError as exc:
            return AssistantResponse(f"Plan rejected: {exc}", False, "invalid")

        completed: list[dict[str, Any]] = []
        for index, action in enumerate(plan.steps, 1):
            if self._cancelled():
                response = self._cancel_response()
                response.data = {"completed_steps": completed, "stopped_at": index}
                self.memory.add_message("assistant", response.text, source)
                return response
            result = self._execute(
                action,
                source=source,
                speaker_match=speaker_match,
                speaker_required=speaker_required,
                confirmed=True,
                pin=None,
            )
            completed.append({"step": index, "tool": action.tool, "success": result.success, "message": result.text})
            if not result.success:
                response = AssistantResponse(
                    f"Plan stopped at step {index}; no later steps were started. "
                    f"Completed/attempted {len(completed)} of {len(plan.steps)} step(s).\n{result.text}",
                    False,
                    result.status,
                    data={"steps": completed},
                )
                self.memory.add_message("assistant", response.text, source)
                return response

        response = AssistantResponse(
            f"Plan finished: {len(completed)} of {len(plan.steps)} step(s) reported success.",
            True,
            "completed",
            data={"steps": completed},
        )
        self.memory.add_message("assistant", response.text, source)
        return response

    def _execute(
        self,
        action: ActionRequest,
        *,
        source: str,
        speaker_match: bool | None,
        speaker_required: bool,
        confirmed: bool,
        pin: str | None,
    ) -> AssistantResponse:
        if self._cancelled():
            return self._cancel_response()
        try:
            validated = self.validator.validate(action.tool, action.arguments)
            action = replace(validated, original_text=action.original_text)
        except ValidationError as exc:
            return AssistantResponse(f"Action validation failed: {exc}", False, "invalid", action)

        decision = self.permissions.check(
            action,
            source=source,
            speaker_match=speaker_match,
            speaker_required=speaker_required,
            confirmed=confirmed,
            pin=pin,
        )
        if not decision.allowed:
            return AssistantResponse(
                decision.message,
                False,
                decision.status,
                action if decision.status in {"confirmation_required", "pin_required"} else None,
            )

        if self._cancelled():
            return self._cancel_response()

        command_id = self.memory.start_command(
            action.original_text or f"[{action.tool}]", source, action.tool, self._safe_arguments(action)
        )
        try:
            result = self._run_tool(action)
        except AutomationError as exc:
            result = ActionResult.failure(action.tool, str(exc))
        except Exception as exc:  # Last-resort recovery boundary for desktop use.
            LOGGER.exception("Tool %s failed", action.tool)
            result = ActionResult.failure(action.tool, f"Unexpected {type(exc).__name__}: {exc}")
        stored_result = dict(result.data)
        if "content" in stored_result:
            stored_result["content"] = "[content omitted from task history]"
        self.memory.finish_command(command_id, action.tool, result.success, result.message, stored_result)
        return AssistantResponse(
            result.message,
            result.success,
            "completed" if result.success else "failed",
            data=result.data,
        )

    @staticmethod
    def _safe_arguments(action: ActionRequest) -> dict[str, Any]:
        if action.tool == "type_text":
            return {"characters": len(action.arguments.get("text", ""))}
        return dict(action.arguments)

    def _run_tool(self, action: ActionRequest) -> ActionResult:
        args = action.arguments
        tool = action.tool
        if tool == "open_application":
            data = self.apps.open(args["name"])
            return ActionResult(tool, True, f"Launched {data['application']} (PID {data['pid']}).", data)
        if tool == "close_application":
            data = self.apps.close(args["name"])
            message = (
                f"{data['application']} was not running."
                if data["already_closed"]
                else f"Close requested for {data['application']}."
            )
            if data["warnings"]:
                message += " Some processes could not be closed: " + "; ".join(data["warnings"])
            return ActionResult(tool, not data["warnings"], message, data)
        if tool == "open_folder":
            data = self.files.open_folder(args["path"])
            return ActionResult(tool, True, f"Asked Windows to open folder: {data['path']}", data)
        if tool == "open_file":
            data = self.files.open_file(args["path"])
            return ActionResult(tool, True, f"Asked Windows to open file: {data['path']}", data)
        if tool == "search_files":
            data = self.files.search(args["query"], args.get("root", ""), args.get("max_results", 20))
            matches = data["matches"]
            lines = [f"Found {len(matches)} matching file(s).", *matches[:10]]
            if data["truncated"]:
                lines.append("Search stopped at a configured bound; narrow the query for more results.")
            return ActionResult(tool, True, "\n".join(lines), data)
        if tool == "create_folder":
            data = self.files.create_folder(args["path"])
            return ActionResult(tool, True, f"Created folder: {data['path']}", data)
        if tool == "read_text_file":
            data = self.files.read_text(args["path"], args.get("max_chars", 12000))
            suffix = "\n\n[Content truncated at a configured limit]" if data["truncated"] else ""
            return ActionResult(tool, True, f"Contents of {data['path']}:\n\n{data['content']}{suffix}", data)
        if tool == "type_text":
            data = self.windows.type_text(args["text"])
            return ActionResult(tool, True, f"Typed {data['characters']} character(s) into the active application.", data)
        if tool == "volume":
            data = self.windows.volume(args["action"], args.get("value"))
            message = f"Volume set to {data['value']}%." if args["action"] == "set" else f"Volume {args['action']} key sent."
            return ActionResult(tool, True, message, data)
        if tool == "system_info":
            data = self.system.snapshot()
            return ActionResult(tool, True, json.dumps(data, indent=2), data)
        if tool == "memory_search":
            rows = self.memory.search(args["query"], args.get("limit", 20))
            message = "\n".join(f"[{row['kind']}] {row['text']}" for row in rows) or "No matching local memory was found."
            return ActionResult(tool, True, message, {"matches": rows})
        if tool == "remember_preference":
            self.memory.set_preference(args["key"], args["value"])
            return ActionResult(tool, True, f"Saved your approved preference '{args['key']}'.", {"key": args["key"]})
        if tool == "list_preferences":
            rows = self.memory.preferences()
            message = "\n".join(f"{row['key']}: {row['value']}" for row in rows) or "No approved preferences are stored."
            return ActionResult(tool, True, message, {"preferences": rows})
        if tool == "set_personality_mode":
            mode = self.personality.set_mode(args["mode"])
            return ActionResult(
                tool, True, f"Personality mode changed to {mode.label}.", {"mode": mode.key}
            )
        if tool == "clear_memory":
            deleted = self.memory.clear(args["scope"])
            return ActionResult(tool, True, f"Deleted {deleted} local memory record(s) from scope '{args['scope']}'.", {"deleted": deleted})
        raise AutomationError(f"No executor is registered for {tool}")
