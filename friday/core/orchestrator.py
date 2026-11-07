"""Understand -> plan -> validate -> authorize -> execute -> verify -> report."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from friday.automation.app_control import AppControl, AutomationError
from friday.automation.file_manager import FileManager
from friday.automation.system_info import SystemInfo
from friday.automation.windows_control import WindowsControl
from friday.core.intent_parser import IntentParser, PlanError
from friday.core.llm_manager import LLMManager, OllamaError
from friday.core.memory import Memory
from friday.core.permissions import PermissionManager
from friday.core.types import ActionRequest, ActionResult, AssistantResponse
from friday.security.action_validator import ActionValidator, ValidationError

LOGGER = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        *,
        validator: ActionValidator,
        parser: IntentParser,
        llm: LLMManager,
        memory: Memory,
        permissions: PermissionManager,
        apps: AppControl,
        files: FileManager,
        windows: WindowsControl,
        system: SystemInfo,
        llm_enabled: bool = True,
    ) -> None:
        self.validator = validator
        self.parser = parser
        self.llm = llm
        self.memory = memory
        self.permissions = permissions
        self.apps = apps
        self.files = files
        self.windows = windows
        self.system = system
        self.llm_enabled = llm_enabled

    def process(
        self,
        text: str,
        *,
        source: str = "text",
        speaker_match: bool | None = None,
        speaker_required: bool = False,
    ) -> AssistantResponse:
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
                )
                kind, payload = self.parser.parse_llm_plan(raw_plan, cleaned)
                if kind == "tool":
                    action = payload
                else:
                    response = AssistantResponse(
                        str(payload), True, "clarification" if kind == "clarify" else "completed"
                    )
                    self.memory.add_message("assistant", response.text, source)
                    return response
            except (OllamaError, PlanError) as exc:
                response = AssistantResponse(
                    f"I could not use the local model: {exc}\n"
                    "Structured commands such as 'open notepad', 'system info', and "
                    "'find files for report in Downloads' still work.",
                    False,
                    "model_unavailable",
                )
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
        if tool == "clear_memory":
            deleted = self.memory.clear(args["scope"])
            return ActionResult(tool, True, f"Deleted {deleted} local memory record(s) from scope '{args['scope']}'.", {"deleted": deleted})
        raise AutomationError(f"No executor is registered for {tool}")
