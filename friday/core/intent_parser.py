"""Fast deterministic commands plus strict parsing of local-LLM JSON plans."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from friday.core.types import ActionRequest
from friday.security.action_validator import ActionValidator, ValidationError


class PlanError(ValueError):
    pass


class IntentParser:
    def __init__(self, validator: ActionValidator) -> None:
        self.validator = validator

    def parse(self, text: str) -> ActionRequest | None:
        stripped = text.strip()
        patterns: list[tuple[str, str, tuple[str, ...]]] = [
            (r"^(?:open|launch|start)\s+(.+)$", "open_application", ("name",)),
            (r"^(?:close|exit|quit)\s+(.+)$", "close_application", ("name",)),
            (r"^(?:open|show)(?:\s+the)?\s+folder\s+(.+)$", "open_folder", ("path",)),
            (r"^open(?:\s+the)?\s+file\s+(.+)$", "open_file", ("path",)),
            (r"^(?:create|make)(?:\s+a)?(?:\s+new)?\s+folder(?:\s+called|\s+named)?\s+(.+)$", "create_folder", ("path",)),
            (r"^(?:read|open\s+and\s+read)(?:\s+the)?(?:\s+text)?\s+file\s+(.+)$", "read_text_file", ("path",)),
            (r"^type\s+(.+)$", "type_text", ("text",)),
        ]
        # More specific path commands must run before generic open application.
        patterns.sort(key=lambda item: 0 if item[1] in {"open_folder", "open_file", "create_folder", "read_text_file", "type_text"} else 1)
        for pattern, tool, keys in patterns:
            match = re.fullmatch(pattern, stripped, flags=re.IGNORECASE)
            if match:
                values = [value.strip() for value in match.groups()]
                if tool == "type_text":
                    values[0] = self._strip_matching_quotes(stripped[5:].strip())
                return self._action(tool, dict(zip(keys, values, strict=True)), text)

        match = re.fullmatch(r"(?:search|find)(?:\s+local)?\s+files?(?:\s+for|\s+named)?\s+(.+?)(?:\s+in\s+(.+))?", stripped, re.IGNORECASE)
        if match:
            args: dict[str, Any] = {"query": match.group(1).strip()}
            if match.group(2):
                args["root"] = match.group(2).strip()
            return self._action("search_files", args, text)

        match = re.fullmatch(r"(?:set\s+)?volume\s*(?:to)?\s*(\d{1,3})(?:\s*percent)?", stripped, re.IGNORECASE)
        if match:
            return self._action("volume", {"action": "set", "value": int(match.group(1))}, text)
        match = re.fullmatch(r"(?:turn\s+)?volume\s+(up|down)|mute(?:\s+volume)?", stripped, re.IGNORECASE)
        if match:
            return self._action("volume", {"action": match.group(1).lower() if match.group(1) else "mute"}, text)

        if re.fullmatch(r"(?:system|pc|computer)\s+(?:info|information|status)|system\s+info", stripped, re.IGNORECASE):
            return self._action("system_info", {}, text)
        if re.fullmatch(r"(?:list|show)(?:\s+my)?\s+preferences", stripped, re.IGNORECASE):
            return self._action("list_preferences", {}, text)
        match = re.fullmatch(r"remember\s+(?:that\s+)?my\s+([\w .-]+)\s+is\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("remember_preference", {"key": match.group(1), "value": match.group(2)}, text)
        match = re.fullmatch(r"(?:search|find)\s+memory\s+(?:for\s+)?(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("memory_search", {"query": match.group(1)}, text)
        match = re.fullmatch(r"(?:clear|delete|forget)\s+(conversations|commands|preferences|memory|everything)", stripped, re.IGNORECASE)
        if match:
            scope = match.group(1).lower()
            return self._action("clear_memory", {"scope": "all" if scope in {"memory", "everything"} else scope}, text)
        return None

    @staticmethod
    def _strip_matching_quotes(value: str) -> str:
        pairs = {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}
        for opening, closing in pairs:
            if len(value) >= 2 and value.startswith(opening) and value.endswith(closing):
                return value[1:-1]
        return value

    def _action(self, tool: str, arguments: dict[str, Any], original: str) -> ActionRequest:
        action = self.validator.validate(tool, arguments)
        return replace(action, original_text=original)

    def parse_llm_plan(self, content: str, original_text: str) -> tuple[str, Any]:
        decoder = json.JSONDecoder()
        for index, character in enumerate(content):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(content[index:])
            except json.JSONDecodeError:
                continue
            if not isinstance(candidate, dict):
                continue
            kind = candidate.get("type")
            if kind == "tool":
                tool_name = candidate.get("tool")
                if not isinstance(tool_name, str):
                    raise PlanError("The local model omitted a valid tool name")
                try:
                    action = self.validator.validate(tool_name, candidate.get("arguments"))
                except ValidationError as exc:
                    raise PlanError(f"The local model proposed an invalid action: {exc}") from exc
                return "tool", replace(action, original_text=original_text)
            if kind in {"chat", "clarify"} and isinstance(candidate.get("content"), str):
                text = candidate["content"].strip()
                if text:
                    return kind, text[:4000]
        raise PlanError("The local model did not return a valid FRIDAY JSON plan")
