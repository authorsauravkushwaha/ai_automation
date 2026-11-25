"""Least-privilege specialist worker routing integrated with AgentRegistry and audit trace."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from hinata.core.types import ActionRequest, ActionResult
from hinata.database.database import Database
from hinata.security.redaction import redact_value


class DelegationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Worker:
    name: str
    allowed_tools: frozenset[str]

    def execute(self, action: ActionRequest, runner: Callable[[ActionRequest], ActionResult]) -> ActionResult:
        if action.tool not in self.allowed_tools:
            raise DelegationError(f"Worker {self.name} cannot use tool {action.tool}")
        result = runner(action)
        if not isinstance(result, ActionResult) or result.tool != action.tool or not isinstance(result.success, bool):
            raise DelegationError("Worker returned an invalid or mismatched result")
        return result


WORKERS = MappingProxyType({
    "research": Worker(
        "research",
        frozenset(
            {
                "web_search",
                "fetch_webpage",
                "list_installed_apps",
                "set_owner_name",
                "owner_name",
                "knowledge_list",
                "knowledge_sync",
                "knowledge_index",
                "knowledge_index_semantic",
                "knowledge_remove",
                "knowledge_search",
                "knowledge_semantic_search",
                "knowledge_hybrid_search",
                "knowledge_answer",
                "list_directory",
                "search_files",
                "search_file_content",
                "read_text_file",
                "compare_files",
            }
        ),
    ),
    "coding": Worker(
        "coding",
        frozenset(
            {
                "read_text_file",
                "code_inspect",
                "inspect_repository_imports",
                "workspace_list",
                "workspace_read",
                "workspace_search",
                "workspace_diff",
                "workspace_propose_patch",
                "workspace_apply_patch",
                "workspace_revert_patch",
                "workspace_symbol_index",
                "run_project_tests",
                "run_linter",
                "run_type_checker",
                "build_project",
                "write_code",
            }
        ),
    ),
    "memory": Worker(
        "memory",
        frozenset({"memory_search", "remember_preference", "list_preferences", "clear_memory"}),
    ),
    "execution": Worker(
        "execution",
        frozenset(
            {
                "open_application",
                "browser_search",
                "scroll_window",
                "close_application",
                "open_folder",
                "open_file",
                "create_folder",
                "write_text_file",
                "copy_file",
                "move_file",
                "archive_files",
                "quarantine_delete_file",
                "type_text",
                "volume",
                "system_info",
                "hardware_profile",
                "ai_fleet_profile",
                "generate_image",
                "set_personality_mode",
                "executive_briefing",
                "list_windows",
                "focus_window",
                "snap_window",
                "send_hotkey",
                "read_clipboard",
                "write_clipboard",
                "capture_screen",
                "list_processes",
                "power_control",
                "open_website",
                "inspect_forms",
                "save_identity",
                "list_identities",
                "get_identity",
                "delete_identity",
                "web_autofill",
                "web_fill_field",
                "web_click",
                "scrape_tables",
                "scrape_lists",
                "monitor_price",
                "crawl_pages",
                "scrape_export",
                "analyze_storage",
                "find_duplicates",
                "organize_folder",
                "batch_rename",
                "convert_file",
                "clean_temp",
                "draft_email",
                "list_drafts",
                "email_template_info",
                "create_reminder",
                "list_reminders",
                "broadcast_message",
                "health_check",
                "network_diag",
                "backup_create",
                "quick_cleanup",
                "process_search",
                "deep_research",
                "compare_topics",
                "summarize_url",
                "git_status",
                "git_diff",
                "git_commit",
                "code_review",
                "dependency_audit",
                "dev_run_tests",
            }
        ),
    ),
})
CODE_SUFFIXES = frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".cpp", ".h", ".rs", ".go", ".java"})


class WorkerCoordinator:
    def __init__(self, database: Database) -> None:
        self.database = database
        with database.connect() as connection:
            from hinata.database.migrations import apply_migrations
            apply_migrations(connection)

    def route(self, action: ActionRequest) -> Worker:
        if (
            action.tool in WORKERS["coding"].allowed_tools
            and action.tool != "read_text_file"
        ) or (
            action.tool == "read_text_file"
            and Path(action.arguments.get("path", "")).suffix.lower() in CODE_SUFFIXES
        ):
            return WORKERS["coding"]
        matches = [worker for name, worker in WORKERS.items() if name != "coding" and action.tool in worker.allowed_tools]
        if len(matches) != 1:
            raise DelegationError("No unique worker is registered for this tool")
        return matches[0]

    def execute(
        self,
        action: ActionRequest,
        runner: Callable[[ActionRequest], ActionResult],
        *,
        run_id: str = "",
        task_id: str = "",
        step_id: str = "",
        correlation_id: str = "",
        model_id: str = "",
        retry_count: int = 0,
    ) -> ActionResult:
        worker = self.route(action)
        started = time.monotonic()
        event_id = self.database.execute(
            """INSERT INTO agent_events(
                worker, tool, status, run_id, task_id, step_id, correlation_id,
                model_id, retry_count
            ) VALUES(?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?)""",
            (
                worker.name,
                action.tool,
                run_id,
                task_id,
                step_id,
                correlation_id,
                model_id[:160],
                int(retry_count),
            ),
        )
        try:
            result = worker.execute(action, runner)
        except Exception as exc:
            dur_ms = round((time.monotonic() - started) * 1000.0, 3)
            self.database.execute(
                "UPDATE agent_events SET status='FAILED', duration_ms=?, error_code=? WHERE id=?",
                (dur_ms, type(exc).__name__[:80], event_id),
            )
            raise
        dur_ms = round((time.monotonic() - started) * 1000.0, 3)
        details: dict[str, Any] = {"success": result.success, "status": result.status}
        if action.tool not in {
            "read_text_file",
            "knowledge_search",
            "knowledge_semantic_search",
            "knowledge_hybrid_search",
            "knowledge_answer",
            "memory_search",
            "type_text",
        }:
            details["arg_keys"] = sorted(action.arguments.keys())
        self.database.execute(
            "UPDATE agent_events SET status=?, duration_ms=?, details_json=? WHERE id=?",
            (
                "COMPLETED" if result.success else "FAILED",
                dur_ms,
                json.dumps(redact_value(details), ensure_ascii=False)[:1000],
                event_id,
            ),
        )
        return result

    def recent(self, limit: int = 50) -> list[dict]:
        return self.database.fetch_all("SELECT * FROM agent_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),))
