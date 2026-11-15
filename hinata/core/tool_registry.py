"""Versioned tool manifest registry, idempotency deduplication, and timeout enforcement."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from hinata.core.errors import ClassifiedError, ErrorCategory
from hinata.core.types import ActionRequest, ActionResult
from hinata.database.database import Database
from hinata.security.action_validator import SCHEMAS


@dataclass(frozen=True, slots=True)
class ToolManifest:
    name: str
    version: str
    description: str
    side_effect: str  # read_only, local_write, ui_focus, process_control, destructive
    risk_tier: str  # low, medium, high
    idempotency: str  # idempotent, deduplicated, non_idempotent
    retryable: bool
    timeout_seconds: float
    offline_capable: bool = True
    requires_online: bool = False
    compensation_tool: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "input_schema": SCHEMAS.get(self.name, {}),
        }


DEFAULT_MANIFESTS: tuple[ToolManifest, ...] = (
    ToolManifest("code_inspect", "1.1", "Read-only static Python AST analysis", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("inspect_repository_imports", "1.0", "Build Python module import graph across workspace", "read_only", "low", "idempotent", True, 20.0),
    ToolManifest("knowledge_list", "1.0", "List indexed local knowledge documents", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("knowledge_sync", "1.1", "Synchronize local folder into knowledge index", "local_write", "medium", "idempotent", False, 30.0),
    ToolManifest("knowledge_index_semantic", "1.1", "Import and semantically embed local document", "local_write", "medium", "idempotent", False, 60.0),
    ToolManifest("knowledge_semantic_search", "1.1", "Semantic cosine search over indexed chunks", "read_only", "low", "idempotent", True, 30.0),
    ToolManifest("knowledge_hybrid_search", "1.0", "Calibrated hybrid lexical + semantic search", "read_only", "low", "idempotent", True, 30.0),
    ToolManifest("knowledge_answer", "1.1", "Grounded display-only RAG answer with claim verification", "read_only", "low", "idempotent", True, 60.0),
    ToolManifest("knowledge_index", "1.1", "Import local document into lexical knowledge index", "local_write", "medium", "idempotent", False, 30.0),
    ToolManifest("knowledge_remove", "1.0", "Remove document copy from local index", "local_write", "medium", "idempotent", False, 10.0),
    ToolManifest("knowledge_search", "1.1", "Lexical search over indexed local chunks", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("open_application", "1.0", "Launch allow-listed desktop application", "process_control", "low", "non_idempotent", False, 10.0),
    ToolManifest("close_application", "1.0", "Close allow-listed desktop application", "process_control", "medium", "idempotent", False, 15.0),
    ToolManifest("open_folder", "1.0", "Open permitted folder in Windows Explorer", "ui_focus", "low", "idempotent", False, 10.0),
    ToolManifest("open_file", "1.0", "Open permitted non-executable file", "ui_focus", "medium", "non_idempotent", False, 10.0),
    ToolManifest("list_directory", "1.0", "List files and directories inside permitted root", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("search_files", "1.0", "Search filenames across permitted folders", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("search_file_content", "1.0", "Bounded full-text content search across approved files", "read_only", "low", "idempotent", True, 15.0),
    ToolManifest("create_folder", "1.0", "Create new folder inside permitted root", "local_write", "low", "deduplicated", False, 10.0),
    ToolManifest("read_text_file", "1.1", "Race-resistant text file reader inside permitted root", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("write_text_file", "1.0", "Atomic bounded text file write inside permitted root", "local_write", "medium", "deduplicated", False, 10.0),
    ToolManifest("copy_file", "1.0", "Copy regular file within permitted roots", "local_write", "medium", "deduplicated", False, 15.0),
    ToolManifest("move_file", "1.0", "Move or rename file within permitted roots", "local_write", "medium", "non_idempotent", False, 15.0),
    ToolManifest("compare_files", "1.0", "Unified diff comparison between two permitted text files", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("archive_files", "1.0", "Create bounded .zip archive from permitted folder or file", "local_write", "medium", "deduplicated", False, 25.0),
    ToolManifest("quarantine_delete_file", "1.0", "PIN-gated reversible quarantine delete of regular file", "destructive", "high", "non_idempotent", False, 15.0),
    ToolManifest("type_text", "1.1", "Focus-verified text typing into active Windows window", "ui_focus", "medium", "non_idempotent", False, 20.0),
    ToolManifest("volume", "1.0", "Adjust or mute Windows audio output", "ui_focus", "low", "idempotent", False, 10.0),
    ToolManifest("system_info", "1.0", "Read OS, CPU, RAM, and disk snapshot", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("hardware_profile", "1.0", "Detect CPU/RAM/GPU/power and recommended local LLM profile", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("memory_search", "1.1", "Search local conversations, preferences, and vetted memories", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("remember_preference", "1.1", "Store secret-checked user preference", "local_write", "medium", "idempotent", False, 10.0),
    ToolManifest("list_preferences", "1.0", "List stored user preferences", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("set_personality_mode", "1.0", "Switch conversational style mode", "local_write", "low", "idempotent", True, 5.0),
    ToolManifest("clear_memory", "1.1", "PIN-gated local memory deletion preserving active task checkpoints", "destructive", "high", "idempotent", False, 15.0),
    ToolManifest("workspace_list", "1.0", "Inventory files and metadata in approved coding workspace", "read_only", "low", "idempotent", True, 15.0),
    ToolManifest("workspace_read", "1.0", "Read source file with SHA-256 digest in approved workspace", "read_only", "low", "idempotent", True, 10.0),
    ToolManifest("workspace_search", "1.0", "Search source lines across approved workspace", "read_only", "low", "idempotent", True, 15.0),
    ToolManifest("workspace_diff", "1.0", "Inspect Git status and working tree diff in workspace", "read_only", "low", "idempotent", True, 15.0),
    ToolManifest("workspace_propose_patch", "1.0", "Generate and store validated unified diff patch proposal", "local_write", "low", "deduplicated", True, 15.0),
    ToolManifest("workspace_apply_patch", "1.0", "Apply approved patch with pre-image hash check and backup", "local_write", "medium", "deduplicated", False, 15.0, compensation_tool="workspace_revert_patch"),
    ToolManifest("workspace_revert_patch", "1.0", "Roll back applied patch from stored pre-image backup", "local_write", "medium", "idempotent", False, 15.0),
    ToolManifest("run_project_tests", "1.0", "Run project unit tests in network-disabled sandbox", "read_only", "medium", "idempotent", True, 60.0),
    ToolManifest("run_linter", "1.0", "Run static linter/compile check in isolated sandbox", "read_only", "low", "idempotent", True, 35.0),
    ToolManifest("run_type_checker", "1.0", "Run static type checker in isolated sandbox", "read_only", "low", "idempotent", True, 45.0),
    ToolManifest("workspace_symbol_index", "1.0", "Zero-execution Python AST class/function symbol & call graph index", "read_only", "low", "idempotent", True, 20.0),
)


class ToolRegistry:
    """Central registry of tool manifests, handlers, timeouts, and idempotency keys."""

    def __init__(self, database: Database | None = None) -> None:
        self.database = database
        self._manifests: dict[str, ToolManifest] = {m.name: m for m in DEFAULT_MANIFESTS}
        self._handlers: dict[str, Callable[[ActionRequest], ActionResult]] = {}

    def register(
        self,
        name: str,
        handler: Callable[[ActionRequest], ActionResult],
        manifest: ToolManifest | None = None,
    ) -> None:
        if manifest is not None:
            self._manifests[name] = manifest
        if name not in self._manifests:
            raise ValueError(f"Tool '{name}' requires a ToolManifest")
        self._handlers[name] = handler

    def manifest(self, name: str) -> ToolManifest:
        if name not in self._manifests:
            raise KeyError(f"Unknown tool manifest: {name}")
        return self._manifests[name]

    def manifests(self) -> dict[str, dict[str, Any]]:
        return {name: m.to_dict() for name, m in sorted(self._manifests.items())}

    @staticmethod
    def compute_idempotency_key(task_id: str, step_id: str, action: ActionRequest) -> str:
        payload = json.dumps(
            {"task_id": task_id, "step_id": step_id, "tool": action.tool, "args": action.arguments},
            sort_keys=True,
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def check_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        if not idempotency_key or self.database is None:
            return None
        return self.database.fetch_one(
            "SELECT * FROM tool_idempotency WHERE idempotency_key = ?", (idempotency_key,)
        )

    def mark_effect(
        self,
        idempotency_key: str,
        *,
        task_id: str,
        step_id: str,
        tool: str,
        effect_state: str,
        summary: dict[str, Any] | None = None,
    ) -> None:
        if not idempotency_key or self.database is None:
            return
        self.database.execute(
            """INSERT INTO tool_idempotency(idempotency_key, task_id, step_id, tool, effect_state, result_summary_json)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(idempotency_key) DO UPDATE SET
               effect_state = excluded.effect_state,
               result_summary_json = excluded.result_summary_json,
               updated_at = datetime('now')""",
            (
                idempotency_key,
                task_id,
                step_id,
                tool,
                effect_state,
                json.dumps(summary or {}, ensure_ascii=False, default=str)[:2000],
            ),
        )

    def invoke(
        self,
        action: ActionRequest,
        *,
        fallback_runner: Callable[[ActionRequest], ActionResult] | None = None,
        timeout_override: float | None = None,
        online_mode_enabled: bool = False,
    ) -> ActionResult:
        manifest = self._manifests.get(action.tool)
        if manifest is not None and manifest.requires_online and not online_mode_enabled:
            raise ClassifiedError(
                ErrorCategory.PERMISSION,
                "ERR_OFFLINE_POLICY",
                f"Tool '{action.tool}' requires Online Mode, which is disabled.",
            )
        handler = self._handlers.get(action.tool) or fallback_runner
        if handler is None:
            raise ClassifiedError(
                ErrorCategory.PERMANENT,
                "ERR_NO_EXECUTOR",
                f"No executor is registered for {action.tool}",
            )
        timeout = timeout_override or (manifest.timeout_seconds if manifest else 30.0)
        if timeout <= 0:
            return handler(action)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="hinata-tool") as pool:
            future = pool.submit(handler, action)
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError as exc:
                raise ClassifiedError(
                    ErrorCategory.TIMEOUT,
                    "ERR_TOOL_TIMEOUT",
                    f"Tool '{action.tool}' timed out after {timeout}s",
                    retryable=bool(manifest and manifest.retryable and manifest.idempotency == "idempotent"),
                ) from exc
