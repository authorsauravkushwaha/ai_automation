"""Fast deterministic commands plus strict parsing of local-LLM JSON plans."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from hinata.core.model_reply import extract_reply_object, normalize_plan, plain_text_reply
from hinata.core.types import ActionRequest
from hinata.security.action_validator import ActionValidator, ValidationError


class PlanError(ValueError):
    pass


class IntentParser:
    def __init__(self, validator: ActionValidator) -> None:
        self.validator = validator

    def parse(self, text: str) -> ActionRequest | None:
        stripped = text.strip()
        match = re.fullmatch(r"(?:inspect|analyze) (?:python|code)(?: file)?\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("code_inspect", {"path": match.group(1)}, text)
        if re.fullmatch(r"inspect (?:repository|workspace) imports", stripped, re.IGNORECASE):
            return self._action("inspect_repository_imports", {}, text)
        if re.fullmatch(r"(?:hardware|gpu|device)\s+profile", stripped, re.IGNORECASE):
            return self._action("hardware_profile", {}, text)
        match = re.fullmatch(r"hybrid search(?: knowledge)?(?: for)?\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_hybrid_search", {"query": match.group(1)}, text)
        match = re.fullmatch(r"list (?:directory|folder)\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("list_directory", {"path": match.group(1)}, text)
        match = re.fullmatch(r"search file content(?: for)?\s+(.+?)(?:\s+in\s+(.+))?", stripped, re.IGNORECASE)
        if match:
            c_args: dict[str, Any] = {"query": match.group(1).strip()}
            if match.group(2):
                c_args["root"] = match.group(2).strip()
            return self._action("search_file_content", c_args, text)
        match = re.fullmatch(r"compare files\s+(\S+)\s+(?:and|with)\s+(\S+)", stripped, re.IGNORECASE)
        if match:
            return self._action("compare_files", {"first": match.group(1), "second": match.group(2)}, text)
        if re.fullmatch(r"workspace (?:list|inventory)", stripped, re.IGNORECASE):
            return self._action("workspace_list", {}, text)
        match = re.fullmatch(r"workspace read\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("workspace_read", {"path": match.group(1)}, text)
        match = re.fullmatch(r"workspace search\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("workspace_search", {"query": match.group(1)}, text)
        if re.fullmatch(r"workspace diff", stripped, re.IGNORECASE):
            return self._action("workspace_diff", {}, text)
        if re.fullmatch(r"run workspace linter", stripped, re.IGNORECASE):
            return self._action("run_linter", {}, text)
        if re.fullmatch(r"run workspace type checker", stripped, re.IGNORECASE):
            return self._action("run_type_checker", {}, text)
        if re.fullmatch(r"run workspace tests", stripped, re.IGNORECASE):
            return self._action("run_project_tests", {}, text)
        if re.fullmatch(r"(?:list|show) knowledge(?: documents)?", stripped, re.IGNORECASE):
            return self._action("knowledge_list", {}, text)
        match = re.fullmatch(r"(?:index folder|sync knowledge folder)\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_sync", {"path": match.group(1)}, text)
        match = re.fullmatch(r"(?:semantic index|semantically index) document\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_index_semantic", {"path": match.group(1)}, text)
        match = re.fullmatch(r"(?:semantic search|search semantic knowledge)(?: for)?\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_semantic_search", {"query": match.group(1)}, text)
        match = re.fullmatch(r"ask knowledge\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_answer", {"query": match.group(1)}, text)
        match = re.fullmatch(r"(?:index|import) document\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_index", {"path": match.group(1)}, text)
        match = re.fullmatch(r"remove document from index\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_remove", {"path": match.group(1)}, text)
        match = re.fullmatch(r"search knowledge(?: for)?\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_search", {"query": match.group(1)}, text)
        mode_match = re.fullmatch(
            r"(?:(?:switch|change|set)(?:\s+to)?\s+)?"
            r"(friend|advisor|technical|strategist|professional)(?:\s+mode)?",
            stripped,
            flags=re.IGNORECASE,
        )
        if mode_match:
            return self._action("set_personality_mode", {"mode": mode_match.group(1).lower()}, text)

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
        candidate = extract_reply_object(content)
        # A small model sometimes answers in plain prose.  That is still a usable
        # answer; only an unrelated blob is rejected as an invalid plan.
        if candidate is None:
            prose = plain_text_reply(content)
            if prose is not None:
                return "chat", prose[:4000]
            raise PlanError("The local model did not return a valid HINATA JSON plan")
        kind = candidate.get("type")
        if kind == "plan":
            return "plan", normalize_plan(candidate)
        if kind == "tool":
            tool_name = candidate.get("tool")
            if not isinstance(tool_name, str):
                raise PlanError("The local model omitted a valid tool name")
            try:
                action = self.validator.validate(tool_name, candidate.get("arguments"))
            except ValidationError as exc:
                raise PlanError(f"The local model proposed an invalid action: {exc}") from exc
            return "tool", replace(action, original_text=original_text)
        if kind in {"chat", "clarify"}:
            text = candidate.get("content")
            if isinstance(text, str) and text.strip():
                return kind, text.strip()[:4000]
            # The model identified a conversational response but chose another
            # common text key.  Recover display text only; actions never reach
            # this branch and remain strictly validated above.
            salvaged = plain_text_reply(content)
            if salvaged:
                return kind, salvaged[:4000]
        raise PlanError("The local model did not return a valid HINATA JSON plan")
