"""Fixed, least-privilege specialist tool dispatch, invoked after authorization.

Workers do not prompt models, delegate recursively, or change permissions. They
route already validated, authorized actions to the existing tool implementation.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from hinata.core.types import ActionRequest, ActionResult
from hinata.database.database import Database


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
    "research": Worker("research", frozenset({"knowledge_list", "knowledge_sync", "knowledge_index", "knowledge_index_semantic",
                                                "knowledge_remove", "knowledge_search", "knowledge_semantic_search", "knowledge_answer",
                                                "search_files", "read_text_file"})),
    "coding": Worker("coding", frozenset({"read_text_file", "code_inspect"})),
    "memory": Worker("memory", frozenset({"memory_search", "remember_preference", "list_preferences", "clear_memory"})),
    "execution": Worker("execution", frozenset({"open_application", "close_application", "open_folder", "open_file", "create_folder",
                                                  "type_text", "volume", "system_info", "set_personality_mode"})),
})
CODE_SUFFIXES = frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".cpp", ".h", ".rs", ".go", ".java"})


class WorkerCoordinator:
    def __init__(self, database: Database) -> None:
        self.database = database
        with database.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT (datetime('now')),
                worker TEXT NOT NULL, tool TEXT NOT NULL, status TEXT NOT NULL
            )""")

    def route(self, action: ActionRequest) -> Worker:
        if action.tool == "code_inspect" or (action.tool == "read_text_file" and Path(action.arguments.get("path", "")).suffix.lower() in CODE_SUFFIXES):
            return WORKERS["coding"]
        matches = [worker for name, worker in WORKERS.items() if name != "coding" and action.tool in worker.allowed_tools]
        if len(matches) != 1:
            raise DelegationError("No unique worker is registered for this tool")
        return matches[0]

    def execute(self, action: ActionRequest, runner: Callable[[ActionRequest], ActionResult]) -> ActionResult:
        worker = self.route(action)
        event_id = self.database.execute("INSERT INTO agent_events(worker, tool, status) VALUES(?, ?, 'RUNNING')",
                                         (worker.name, action.tool))
        try:
            result = worker.execute(action, runner)
        except Exception:
            self.database.execute("UPDATE agent_events SET status='FAILED' WHERE id=?", (event_id,))
            raise
        self.database.execute("UPDATE agent_events SET status=? WHERE id=?", ("COMPLETED" if result.success else "FAILED", event_id))
        return result

    def recent(self, limit: int = 50) -> list[dict]:
        return self.database.fetch_all("SELECT * FROM agent_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),))
