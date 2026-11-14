"""Strict, dependency-free validation for every model-selectable tool."""

from __future__ import annotations

import re
from typing import Any

from hinata.core.types import ActionRequest


class ValidationError(ValueError):
    pass


SCHEMAS: dict[str, dict[str, Any]] = {
    "code_inspect": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_list": {"required": [], "properties": {}},
    "knowledge_sync": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_index_semantic": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_semantic_search": {"required": ["query"], "properties": {"query": ("string", 1, 200), "limit": ("integer", 1, 10)}},
    "knowledge_answer": {"required": ["query"], "properties": {"query": ("string", 1, 200)}},
    "knowledge_index": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_remove": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_search": {"required": ["query"], "properties": {"query": ("string", 1, 200), "limit": ("integer", 1, 10)}},
    "open_application": {"required": ["name"], "properties": {"name": ("string", 1, 64)}},
    "close_application": {"required": ["name"], "properties": {"name": ("string", 1, 64)}},
    "open_folder": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "open_file": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "search_files": {
        "required": ["query"],
        "properties": {
            "query": ("string", 1, 200),
            "root": ("string", 0, 1024),
            "max_results": ("integer", 1, 50),
        },
    },
    "create_folder": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "read_text_file": {
        "required": ["path"],
        "properties": {"path": ("string", 1, 1024), "max_chars": ("integer", 1, 20000)},
    },
    "type_text": {"required": ["text"], "properties": {"text": ("string", 1, 2000)}},
    "volume": {
        "required": ["action"],
        "properties": {
            "action": ("enum", ["up", "down", "mute", "set"]),
            "value": ("integer", 0, 100),
        },
    },
    "system_info": {"required": [], "properties": {}},
    "memory_search": {
        "required": ["query"],
        "properties": {"query": ("string", 1, 200), "limit": ("integer", 1, 50)},
    },
    "remember_preference": {
        "required": ["key", "value"],
        "properties": {"key": ("string", 1, 50), "value": ("string", 1, 500)},
    },
    "list_preferences": {"required": [], "properties": {}},
    "set_personality_mode": {
        "required": ["mode"],
        "properties": {
            "mode": ("enum", ["friend", "advisor", "technical", "strategist", "professional"])
        },
    },
    "clear_memory": {
        "required": ["scope"],
        "properties": {"scope": ("enum", ["conversations", "commands", "preferences", "all"])},
    },
}


class ActionValidator:
    def validate(self, tool: str, arguments: dict[str, Any] | None) -> ActionRequest:
        if tool not in SCHEMAS:
            raise ValidationError(f"Unknown tool: {tool}")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValidationError("Tool arguments must be a JSON object")
        schema = SCHEMAS[tool]
        properties = schema["properties"]
        unknown = set(arguments) - set(properties)
        if unknown:
            raise ValidationError(f"Unsupported argument(s): {', '.join(sorted(unknown))}")
        missing = [key for key in schema["required"] if key not in arguments]
        if missing:
            raise ValidationError(f"Missing argument(s): {', '.join(missing)}")

        clean: dict[str, Any] = {}
        for key, value in arguments.items():
            rule = properties[key]
            if rule[0] == "string":
                if not isinstance(value, str) or not rule[1] <= len(value) <= rule[2]:
                    raise ValidationError(f"{key} must be a string of length {rule[1]}..{rule[2]}")
                if "\x00" in value:
                    raise ValidationError(f"{key} contains a null byte")
                cleaned = value.strip() if key != "text" else value
                if len(cleaned) < rule[1]:
                    raise ValidationError(f"{key} must not be empty or whitespace")
                clean[key] = cleaned
            elif rule[0] == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValidationError(f"{key} must be an integer")
                if not rule[1] <= value <= rule[2]:
                    raise ValidationError(f"{key} must be between {rule[1]} and {rule[2]}")
                clean[key] = value
            elif rule[0] == "enum":
                if value not in rule[1]:
                    raise ValidationError(f"{key} must be one of: {', '.join(rule[1])}")
                clean[key] = value
        if tool == "volume" and clean.get("action") == "set" and "value" not in clean:
            raise ValidationError("value is required when volume action is 'set'")
        if tool == "volume" and clean.get("action") != "set" and "value" in clean:
            raise ValidationError("value is only valid when volume action is 'set'")
        if tool == "remember_preference" and not re.fullmatch(r"[\w .-]+", clean["key"]):
            raise ValidationError("Preference keys may contain letters, digits, space, ., _, or -")
        return ActionRequest(tool=tool, arguments=clean)

    def tool_descriptions(self) -> dict[str, dict[str, Any]]:
        return SCHEMAS.copy()
