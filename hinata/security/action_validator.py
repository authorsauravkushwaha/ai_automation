"""Strict, dependency-free validation for every model-selectable tool."""

from __future__ import annotations

import re
from typing import Any

from hinata.core.types import ActionRequest


class ValidationError(ValueError):
    pass


SCHEMAS: dict[str, dict[str, Any]] = {
    "web_search": {"required": ["query"], "properties": {"query": ("string", 1, 1000), "max_results": ("integer", 1, 12)}},
    "browser_search": {"required": ["query"], "properties": {"query": ("string", 1, 1000), "browser": ("string", 1, 80), "engine": ("enum", ["duckduckgo", "google", "bing"])}},
    "scroll_window": {"required": ["direction"], "properties": {"direction": ("enum", ["up", "down"]), "steps": ("integer", 1, 10)}},
    "fetch_webpage": {"required": ["url"], "properties": {"url": ("string", 8, 2048), "max_chars": ("integer", 200, 15000)}},
    "list_installed_apps": {"required": [], "properties": {"query": ("string", 0, 100), "limit": ("integer", 1, 200)}},
    "write_code": {"required": ["task"], "properties": {"task": ("string", 1, 4000), "language": ("string", 1, 40), "output_path": ("string", 0, 1024), "overwrite": ("boolean",)}},
    "set_owner_name": {"required": ["name"], "properties": {"name": ("string", 1, 160)}},
    "owner_name": {"required": [], "properties": {}},
    "code_inspect": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_list": {"required": [], "properties": {}},
    "knowledge_sync": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_index_semantic": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_semantic_search": {"required": ["query"], "properties": {"query": ("string", 1, 200), "limit": ("integer", 1, 10)}},
    "knowledge_hybrid_search": {"required": ["query"], "properties": {"query": ("string", 1, 200), "limit": ("integer", 1, 10)}},
    "knowledge_answer": {"required": ["query"], "properties": {"query": ("string", 1, 200)}},
    "knowledge_index": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_remove": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "knowledge_search": {"required": ["query"], "properties": {"query": ("string", 1, 200), "limit": ("integer", 1, 10)}},
    "open_application": {"required": ["name"], "properties": {"name": ("string", 1, 64)}},
    "close_application": {"required": ["name"], "properties": {"name": ("string", 1, 64)}},
    "open_folder": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "open_file": {"required": ["path"], "properties": {"path": ("string", 1, 1024)}},
    "list_directory": {
        "required": [],
        "properties": {"path": ("string", 0, 1024), "max_entries": ("integer", 1, 200)},
    },
    "search_files": {
        "required": ["query"],
        "properties": {
            "query": ("string", 1, 200),
            "root": ("string", 0, 1024),
            "max_results": ("integer", 1, 50),
        },
    },
    "search_file_content": {
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
    "write_text_file": {
        "required": ["path", "content"],
        "properties": {
            "path": ("string", 1, 1024),
            "content": ("string", 0, 100000),
            "overwrite": ("boolean",),
        },
    },
    "copy_file": {
        "required": ["source", "destination"],
        "properties": {"source": ("string", 1, 1024), "destination": ("string", 1, 1024)},
    },
    "move_file": {
        "required": ["source", "destination"],
        "properties": {"source": ("string", 1, 1024), "destination": ("string", 1, 1024)},
    },
    "compare_files": {
        "required": ["first", "second"],
        "properties": {"first": ("string", 1, 1024), "second": ("string", 1, 1024)},
    },
    "archive_files": {
        "required": ["source", "archive_path"],
        "properties": {"source": ("string", 1, 1024), "archive_path": ("string", 1, 1024)},
    },
    "quarantine_delete_file": {
        "required": ["path"],
        "properties": {"path": ("string", 1, 1024)},
    },
    "type_text": {
        "required": ["text"],
        "properties": {"text": ("string", 1, 2000), "target_window": ("string", 0, 200)},
    },
    "volume": {
        "required": ["action"],
        "properties": {
            "action": ("enum", ["up", "down", "mute", "set"]),
            "value": ("integer", 0, 100),
        },
    },
    "system_info": {"required": [], "properties": {}},
    "hardware_profile": {"required": [], "properties": {}},
    "ai_fleet_profile": {"required": [], "properties": {}},
    "generate_image": {
        "required": ["prompt", "output_path"],
        "properties": {
            "prompt": ("string", 1, 600),
            "output_path": ("string", 1, 1024),
            "style": ("enum", ["jarvis_hud", "cyberpunk", "minimal", "blueprint", "sunset"]),
            "width": ("integer", 256, 2048),
            "height": ("integer", 256, 2048),
        },
    },
    "build_project": {
        "required": ["project_dir", "specification"],
        "properties": {
            "project_dir": ("string", 1, 1024),
            "specification": ("string", 1, 2000),
            "template": ("enum", ["python_cli", "web_dashboard"]),
            "run_verification": ("boolean",),
        },
    },
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
            "mode": (
                "enum",
                ["friend", "advisor", "technical", "strategist", "professional", "jarvis"],
            )
        },
    },
    "clear_memory": {
        "required": ["scope"],
        "properties": {
            "scope": (
                "enum",
                [
                    "conversations",
                    "commands",
                    "preferences",
                    "working",
                    "episodic",
                    "semantic",
                    "procedural",
                    "project",
                    "all",
                ],
            )
        },
    },
    # Sandboxed coding workspace tools
    "workspace_list": {
        "required": [],
        "properties": {"workspace": ("string", 0, 512), "max_files": ("integer", 1, 500)},
    },
    "workspace_read": {
        "required": ["path"],
        "properties": {
            "path": ("string", 1, 1024),
            "workspace": ("string", 0, 512),
            "max_chars": ("integer", 1, 50000),
        },
    },
    "workspace_search": {
        "required": ["query"],
        "properties": {
            "query": ("string", 1, 200),
            "workspace": ("string", 0, 512),
            "max_results": ("integer", 1, 50),
        },
    },
    "workspace_diff": {
        "required": [],
        "properties": {"workspace": ("string", 0, 512)},
    },
    "workspace_propose_patch": {
        "required": ["path", "new_content"],
        "properties": {
            "path": ("string", 1, 1024),
            "new_content": ("string", 0, 150000),
            "title": ("string", 1, 200),
            "workspace": ("string", 0, 512),
        },
    },
    "workspace_apply_patch": {
        "required": ["patch_id"],
        "properties": {"patch_id": ("string", 4, 64)},
    },
    "workspace_revert_patch": {
        "required": ["patch_id"],
        "properties": {"patch_id": ("string", 4, 64)},
    },
    "run_project_tests": {
        "required": [],
        "properties": {"workspace": ("string", 0, 512), "target": ("string", 0, 256)},
    },
    "run_linter": {
        "required": [],
        "properties": {"workspace": ("string", 0, 512)},
    },
    "run_type_checker": {
        "required": [],
        "properties": {"workspace": ("string", 0, 512)},
    },
    "inspect_repository_imports": {
        "required": [],
        "properties": {"workspace": ("string", 0, 512)},
    },
    "workspace_symbol_index": {
        "required": [],
        "properties": {
            "workspace": ("string", 0, 512),
            "query": ("string", 0, 128),
            "max_symbols": ("integer", 1, 500),
        },
    },
    # J.A.R.V.I.S. desktop cockpit & tactical awareness schemas
    "executive_briefing": {
        "required": [],
        "properties": {},
    },
    "list_windows": {
        "required": [],
        "properties": {
            "filter": ("string", 0, 120),
            "limit": ("integer", 1, 100),
        },
    },
    "focus_window": {
        "required": ["title"],
        "properties": {"title": ("string", 1, 150)},
    },
    "snap_window": {
        "required": ["title", "layout"],
        "properties": {
            "title": ("string", 1, 150),
            "layout": ("enum", ["left", "right", "maximize", "minimize", "restore"]),
        },
    },
    "send_hotkey": {
        "required": ["shortcut"],
        "properties": {
            "shortcut": (
                "enum",
                [
                    "copy",
                    "paste",
                    "cut",
                    "save",
                    "undo",
                    "redo",
                    "select_all",
                    "find",
                    "new_tab",
                    "close_tab",
                    "switch_tab",
                    "enter",
                    "escape",
                ],
            ),
            "target_window": ("string", 0, 150),
        },
    },
    "read_clipboard": {
        "required": [],
        "properties": {"max_chars": ("integer", 1, 20000)},
    },
    "write_clipboard": {
        "required": ["text"],
        "properties": {"text": ("string", 1, 50000)},
    },
    "capture_screen": {
        "required": ["path"],
        "properties": {
            "path": ("string", 1, 512),
            "analyze_prompt": ("string", 0, 300),
        },
    },
    "list_processes": {
        "required": [],
        "properties": {
            "filter": ("string", 0, 120),
            "limit": ("integer", 1, 100),
        },
    },
    "power_control": {
        "required": ["action"],
        "properties": {
            "action": ("enum", ["lock", "sleep", "hibernate"]),
        },
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
                cleaned = value.strip() if key not in {"text", "content", "new_content"} else value
                if len(cleaned) < rule[1]:
                    raise ValidationError(f"{key} must not be empty or whitespace")
                clean[key] = cleaned
            elif rule[0] == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValidationError(f"{key} must be an integer")
                if not rule[1] <= value <= rule[2]:
                    raise ValidationError(f"{key} must be between {rule[1]} and {rule[2]}")
                clean[key] = value
            elif rule[0] == "boolean":
                if not isinstance(value, bool):
                    raise ValidationError(f"{key} must be a boolean")
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
