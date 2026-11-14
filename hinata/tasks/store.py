"""SQLite checkpoints for approved bounded plans. Never resumes side effects."""
from __future__ import annotations

import json
import uuid
from typing import Any

from hinata.core.planner import ExecutionPlan
from hinata.database.database import Database


class TaskStateError(RuntimeError):
    """A task is missing, changed, already claimed, or no longer runnable."""


class TaskStore:
    def __init__(self, database: Database) -> None:
        self.database = database
        with database.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    id TEXT PRIMARY KEY, objective TEXT NOT NULL, source TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS agent_task_steps (
                    task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, tool TEXT NOT NULL, arguments_json TEXT NOT NULL,
                    state TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(task_id, ordinal)
                );
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(agent_tasks)")}
            if "parent_id" not in columns:
                connection.execute("ALTER TABLE agent_tasks ADD COLUMN parent_id TEXT")
            if "revision" not in columns:
                connection.execute("ALTER TABLE agent_tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            if "user_request" not in columns:
                connection.execute("ALTER TABLE agent_tasks ADD COLUMN user_request TEXT NOT NULL DEFAULT ''")
            if "verification_json" not in columns:
                connection.execute("ALTER TABLE agent_tasks ADD COLUMN verification_json TEXT NOT NULL DEFAULT '[]'")
            if "verification_method" not in columns:
                connection.execute("ALTER TABLE agent_tasks ADD COLUMN verification_method TEXT NOT NULL DEFAULT ''")
                if "attestation" in columns:
                    connection.execute("UPDATE agent_tasks SET verification_method='human' WHERE attestation <> ''")
            if "objective_evidence" not in columns:
                connection.execute("ALTER TABLE agent_tasks ADD COLUMN objective_evidence TEXT NOT NULL DEFAULT ''")
            if "attestation" not in columns:
                connection.execute("ALTER TABLE agent_tasks ADD COLUMN attestation TEXT NOT NULL DEFAULT ''")
                # Earlier versions used COMPLETED to mean tool-reported success.
                # Preserve the records, but do not present those as verified tasks.
                connection.execute("UPDATE agent_tasks SET state='VERIFYING' WHERE state='COMPLETED'")

    def create(self, plan: ExecutionPlan, source: str, *, parent_id: str | None = None, revision: int = 0, user_request: str = "") -> str:
        if revision not in {0, 1} or (revision == 0) != (parent_id is None):
            raise ValueError("Only one linked plan revision is supported")
        task_id = uuid.uuid4().hex
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO agent_tasks(id, objective, source, state, parent_id, revision, user_request) VALUES(?, ?, ?, 'AWAITING_APPROVAL', ?, ?, ?)",
                (task_id, plan.objective, source, parent_id, revision, user_request[:4000]),
            )
            connection.executemany(
                "INSERT INTO agent_task_steps(task_id, ordinal, tool, arguments_json, state) VALUES(?, ?, ?, ?, 'QUEUED')",
                [(task_id, i, step.tool, json.dumps(step.arguments, ensure_ascii=True, sort_keys=True),)
                 for i, step in enumerate(plan.steps, 1)],
            )
        return task_id

    def claim(self, task_id: str, plan: ExecutionPlan, source: str) -> None:
        """Atomically claim exactly the plan that was displayed for approval."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT source, state FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
            rows = connection.execute(
                "SELECT tool, arguments_json FROM agent_task_steps WHERE task_id = ? ORDER BY ordinal", (task_id,)
            ).fetchall()
            expected = [(step.tool, step.arguments) for step in plan.steps]
            actual = [(row["tool"], json.loads(row["arguments_json"])) for row in rows]
            if task is None or task["state"] != "AWAITING_APPROVAL" or task["source"] != source or actual != expected:
                raise TaskStateError("Plan is missing, changed, already claimed, or no longer awaiting approval")
            connection.execute("UPDATE agent_tasks SET state='RUNNING', updated_at=datetime('now') WHERE id=?", (task_id,))

    def cancel_pending(self, task_id: str) -> bool:
        with self.database.connect() as connection:
            result = connection.execute(
                "UPDATE agent_tasks SET state='CANCELLED', updated_at=datetime('now') WHERE id=? AND state='AWAITING_APPROVAL'", (task_id,)
            )
            return result.rowcount == 1

    def start_step(self, task_id: str, ordinal: int) -> None:
        with self.database.connect() as connection:
            result = connection.execute("""UPDATE agent_task_steps SET state='RUNNING'
                WHERE task_id=? AND ordinal=? AND state='QUEUED' AND EXISTS
                (SELECT 1 FROM agent_tasks WHERE id=? AND state='RUNNING')""", (task_id, ordinal, task_id))
            if result.rowcount != 1:
                raise TaskStateError("Cannot checkpoint the next task step")

    def finish_step(self, task_id: str, ordinal: int, success: bool, evidence: str) -> None:
        with self.database.connect() as connection:
            result = connection.execute("""UPDATE agent_task_steps SET state=?, evidence=?
                WHERE task_id=? AND ordinal=? AND state='RUNNING' AND EXISTS
                (SELECT 1 FROM agent_tasks WHERE id=? AND state='RUNNING')""",
                ("COMPLETED" if success else "FAILED", evidence[:500], task_id, ordinal, task_id))
            if result.rowcount != 1:
                raise TaskStateError("Cannot checkpoint task result; outcome may be uncertain")

    def finish(self, task_id: str, state: str) -> None:
        if state not in {"FAILED", "CANCELLED"}:
            raise ValueError("Invalid final task state")
        with self.database.connect() as connection:
            result = connection.execute("""UPDATE agent_tasks SET state=?, updated_at=datetime('now')
                WHERE id=? AND state='RUNNING'""", (state, task_id))
            if result.rowcount != 1:
                raise TaskStateError("Cannot finalize task")

    def begin_verification(self, task_id: str, checks: list[dict[str, Any]]) -> None:
        """Never claim objective completion from tool return codes alone."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT state FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            steps = connection.execute("SELECT ordinal, tool, state FROM agent_task_steps WHERE task_id=? ORDER BY ordinal", (task_id,)).fetchall()
            if not task or task["state"] != "RUNNING" or not steps or any(step["state"] != "COMPLETED" for step in steps):
                raise TaskStateError("Task is not ready for verification")
            if len(checks) != len(steps) or any(
                c.get("step") != step["ordinal"] or c.get("tool") != step["tool"] or c.get("status") not in {"verified", "unknown", "contradicted"}
                for c, step in zip(checks, steps, strict=True)
            ):
                raise TaskStateError("Verification evidence does not match the task")
            state = "FAILED" if any(c["status"] == "contradicted" for c in checks) else "VERIFYING"
            connection.execute("UPDATE agent_tasks SET state=?, verification_json=?, updated_at=datetime('now') WHERE id=?",
                               (state, json.dumps(checks), task_id))

    def complete_folder_objective(self, task_id: str, proof: dict[str, Any]) -> bool:
        """Record a narrowly scoped verifier result only against its exact checkpoint."""
        if set(proof) == {"kind", "path", "requested", "tool_path"} and proof["kind"] == "folder_exists":
            tool_paths = [proof["tool_path"]]
        elif (set(proof) == {"kind", "paths", "requested", "tool_paths"}
              and proof["kind"] == "folders_exist" and isinstance(proof["paths"], list)
              and isinstance(proof["tool_paths"], list)
              and 2 <= len(proof["paths"]) == len(proof["tool_paths"]) <= 5):
            tool_paths = proof["tool_paths"]
        else:
            raise TaskStateError("Unsupported objective proof")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute("SELECT state, user_request, verification_json FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            rows = connection.execute("SELECT tool, arguments_json, state FROM agent_task_steps WHERE task_id=? ORDER BY ordinal", (task_id,)).fetchall()
            if not task or task["state"] != "VERIFYING":
                return False
            checks = json.loads(task["verification_json"])
            if (task["user_request"] != proof["requested"] or len(rows) != len(tool_paths) or
                len(checks) != len(rows) or any(
                    row["tool"] != "create_folder" or row["state"] != "COMPLETED" or
                    json.loads(row["arguments_json"]) != {"path": tool_path} or
                    check.get("status") != "verified" or check.get("tool") != row["tool"] or
                    check.get("step") != ordinal
                    for ordinal, (row, tool_path, check) in enumerate(zip(rows, tool_paths, checks, strict=True), 1)
                )):
                raise TaskStateError("Objective proof does not match task evidence")
            result = connection.execute("""UPDATE agent_tasks SET state='COMPLETED', verification_method='folder_postcondition',
                objective_evidence=?, updated_at=datetime('now') WHERE id=? AND state='VERIFYING'""", (json.dumps(proof), task_id))
            return result.rowcount == 1

    def attest(self, task_id: str, evidence: str, *, satisfied: bool, checks: list[dict[str, Any]]) -> bool:
        if not isinstance(satisfied, bool):
            raise ValueError("Review decision must be a boolean")
        if not isinstance(evidence, str) or not 10 <= len(evidence.strip()) <= 1000:
            raise ValueError("Provide 10..1000 characters of observed evidence")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            steps = connection.execute("SELECT ordinal, tool, state FROM agent_task_steps WHERE task_id=? ORDER BY ordinal", (task_id,)).fetchall()
            if not steps or len(checks) != len(steps) or any(
                c.get("step") != step["ordinal"] or c.get("tool") != step["tool"] or
                c.get("status") not in {"verified", "unknown", "contradicted"} or step["state"] != "COMPLETED"
                for c, step in zip(checks, steps, strict=True)
            ):
                raise TaskStateError("Verification evidence does not match the task")
            if satisfied and any(c["status"] == "contradicted" for c in checks):
                raise TaskStateError("Contradicted checks cannot be marked complete")
            result = connection.execute("""UPDATE agent_tasks SET state=?, attestation=?, verification_json=?, verification_method='human', updated_at=datetime('now')
                WHERE id=? AND state='VERIFYING'""", ("COMPLETED" if satisfied else "FAILED", evidence.strip(), json.dumps(checks), task_id))
            return result.rowcount == 1

    def get(self, task_id: str) -> dict[str, Any] | None:
        task = self.database.fetch_one("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        if task is None:
            return None
        task["steps"] = self.database.fetch_all(
            "SELECT ordinal, tool, arguments_json, state, evidence FROM agent_task_steps WHERE task_id=? ORDER BY ordinal", (task_id,)
        )
        return task

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM agent_tasks ORDER BY rowid DESC LIMIT ?", (max(1, min(limit, 100)),)
        )
