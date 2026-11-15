"""SQLite checkpoints, plan-hash approvals, leases, reconciliation, and task lifecycle.

Safety invariants preserved:
- Interrupted ``RUNNING`` side effects are NEVER automatically replayed or resumed.
- Crash recovery transitions stale ``RUNNING`` steps to ``UNKNOWN_OUTCOME`` and
  runs read-only postcondition reconciliation before asking for human review when
  the outcome cannot be proven.
- Approval binds cryptographically to ``plan_hash``, ``source``, and version so
  neither duplicate claims nor post-preview plan mutations can execute.
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from hinata.core.planner import ExecutionPlan, PlanNode
from hinata.database.database import Database
from hinata.security.redaction import redact_value


class TaskStateError(RuntimeError):
    """A task is missing, changed, already claimed, or no longer runnable."""


STEP_STATES = frozenset(
    {
        "BLOCKED",
        "QUEUED",
        "READY",
        "AWAITING_APPROVAL",
        "RUNNING",
        "OBSERVING",
        "VERIFYING",
        "SUCCEEDED",
        "COMPLETED",  # Retained alias for backward compatibility with existing tests
        "FAILED",
        "UNKNOWN_OUTCOME",
        "CANCELLED",
        "SKIPPED",
    }
)

TASK_STATES = frozenset(
    {
        "CREATED",
        "DISCOVERING",
        "CLARIFYING",
        "PLANNING",
        "AWAITING_APPROVAL",
        "READY",
        "RUNNING",
        "WAITING_FOR_DEPENDENCY",
        "WAITING_FOR_USER",
        "PAUSED",
        "REPLANNING",
        "VERIFYING",
        "REPAIRING",
        "SUCCEEDED",
        "PARTIAL",
        "COMPLETED",  # Retained alias for backward compatibility with existing tests
        "FAILED",
        "BLOCKED",
        "UNKNOWN_OUTCOME",
        "CANCELLED",
    }
)


class TaskStore:
    def __init__(self, database: Database, *, max_revisions: int = 2) -> None:
        self.database = database
        self.max_revisions = max(1, min(max_revisions, 5))
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
            else:
                # Also migrate legacy rows created before migrations recorded verification_method/attestation.
                connection.execute(
                    "UPDATE agent_tasks SET state='VERIFYING' WHERE state='COMPLETED' AND verification_method='' AND attestation=''"
                )
            # Ensure extended runtime columns exist even if TaskStore is initialized against a bare legacy DB.
            from hinata.database.migrations import apply_migrations
            apply_migrations(connection)

    def create(
        self,
        plan: ExecutionPlan,
        source: str,
        *,
        parent_id: str | None = None,
        revision: int = 0,
        user_request: str = "",
        autonomy_mode: str = "APPROVED_PLAN",
        owner: str = "local_owner",
        model_identity: str = "",
    ) -> str:
        if revision < 0 or revision > self.max_revisions or (revision == 0) != (parent_id is None):
            raise ValueError(f"Invalid plan revision={revision} / parent_id combination")
        task_id = uuid.uuid4().hex
        phash = plan.plan_hash()
        contract_dict = plan.contract.to_dict() if plan.contract else {}
        effective_nodes = (
            plan.nodes
            if plan.nodes
            else tuple(
                PlanNode(step_id=f"step_{i}", action=s)
                for i, s in enumerate(plan.steps, 1)
            )
        )
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO agent_tasks(
                    id, objective, source, state, parent_id, revision, user_request,
                    owner, autonomy_mode, plan_hash, contract_json, model_identity
                ) VALUES(?, ?, ?, 'AWAITING_APPROVAL', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    plan.objective,
                    source,
                    parent_id,
                    revision,
                    user_request[:4000],
                    owner[:64],
                    autonomy_mode,
                    phash,
                    json.dumps(contract_dict, ensure_ascii=False),
                    model_identity[:200],
                ),
            )
            connection.executemany(
                """INSERT INTO agent_task_steps(
                    task_id, ordinal, step_id, agent_id, tool, arguments_json,
                    depends_on_json, risk_tier, side_effect, idempotency,
                    timeout_seconds, max_retries, explanation, state
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED')""",
                [
                    (
                        task_id,
                        i,
                        node.step_id,
                        node.agent_id,
                        node.action.tool,
                        json.dumps(node.action.arguments, ensure_ascii=True, sort_keys=True),
                        json.dumps(list(node.depends_on)),
                        node.risk_tier,
                        node.side_effect,
                        node.idempotency,
                        node.timeout_seconds,
                        node.max_retries,
                        node.explanation,
                    )
                    for i, node in enumerate(effective_nodes, 1)
                ],
            )
        return task_id

    def claim(
        self,
        task_id: str,
        plan: ExecutionPlan,
        source: str,
        *,
        approved_by: str = "owner",
        policy: str = "interactive_plan_approval",
        lease_owner: str = "main_worker",
        lease_seconds: float = 120.0,
    ) -> None:
        """Atomically claim and bind approval to the exact plan hash, steps, and source."""
        approval_record = {
            "approved_by": approved_by,
            "policy": policy,
            "plan_hash": plan.plan_hash(),
            "approved_at_epoch": round(time.time(), 3),
            "expires_at_epoch": round(time.time() + 3600.0, 3),
        }
        lease_exp = str(round(time.time() + max(5.0, lease_seconds), 3))
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT source, state, plan_hash, version FROM agent_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT tool, arguments_json FROM agent_task_steps WHERE task_id = ? ORDER BY ordinal", (task_id,)
            ).fetchall()
            expected = [(step.tool, step.arguments) for step in plan.steps]
            actual = [(row["tool"], json.loads(row["arguments_json"])) for row in rows]
            if task is None or task["state"] != "AWAITING_APPROVAL" or task["source"] != source or actual != expected:
                raise TaskStateError("Plan is missing, changed, already claimed, or no longer awaiting approval")
            if task["plan_hash"] and task["plan_hash"] != plan.plan_hash():
                raise TaskStateError("Plan hash changed since approval preview; approval invalidated")
            res = connection.execute(
                """UPDATE agent_tasks SET state='RUNNING', version = version + 1,
                   approval_json = ?, lease_owner = ?, lease_expires_at = ?,
                   updated_at=datetime('now')
                   WHERE id=? AND version=? AND state='AWAITING_APPROVAL'""",
                (json.dumps(approval_record), lease_owner, lease_exp, task_id, task["version"]),
            )
            if res.rowcount != 1:
                raise TaskStateError("Concurrent task claim conflict")

    def heartbeat(self, task_id: str, lease_owner: str = "main_worker", lease_seconds: float = 120.0) -> bool:
        lease_exp = str(round(time.time() + max(5.0, lease_seconds), 3))
        with self.database.connect() as conn:
            res = conn.execute(
                "UPDATE agent_tasks SET lease_expires_at = ?, updated_at=datetime('now') WHERE id = ? AND state = 'RUNNING' AND lease_owner = ?",
                (lease_exp, task_id, lease_owner),
            )
            return res.rowcount == 1

    def pause_task(self, task_id: str, reason: str = "User paused at safe boundary") -> bool:
        """Pause a task ONLY if no step is currently in-flight (RUNNING)."""
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inflight = conn.execute(
                "SELECT 1 FROM agent_task_steps WHERE task_id = ? AND state = 'RUNNING'",
                (task_id,),
            ).fetchone()
            if inflight is not None:
                return False
            res = conn.execute(
                """UPDATE agent_tasks SET state = 'PAUSED', paused_reason = ?,
                   version = version + 1, updated_at = datetime('now')
                   WHERE id = ? AND state IN ('RUNNING', 'AWAITING_APPROVAL')""",
                (reason[:300], task_id),
            )
            return res.rowcount == 1

    def resume_paused_task(self, task_id: str) -> bool:
        """Resume a PAUSED task only if all prior steps have reconciled known outcomes."""
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            uncertain = conn.execute(
                "SELECT 1 FROM agent_task_steps WHERE task_id = ? AND state IN ('RUNNING', 'UNKNOWN_OUTCOME')",
                (task_id,),
            ).fetchone()
            if uncertain is not None:
                return False
            res = conn.execute(
                """UPDATE agent_tasks SET state = 'RUNNING', paused_reason = '',
                   version = version + 1, updated_at = datetime('now')
                   WHERE id = ? AND state = 'PAUSED'""",
                (task_id,),
            )
            return res.rowcount == 1

    def reconcile_interrupted_task(
        self,
        task_id: str,
        reconciler: Callable[[str, dict[str, Any]], tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Explicit owner-triggered crash reconciliation. Never replays actions!

        Marks interrupted ``RUNNING`` steps as ``UNKNOWN_OUTCOME`` unless a read-only
        ``reconciler(tool, args)`` proves ``('verified', detail)`` or ``('not_applied', detail)``.
        """
        task = self.get(task_id)
        if task is None:
            raise TaskStateError("Task does not exist")
        step_reports: list[dict[str, Any]] = []
        any_unknown = False
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            steps = conn.execute(
                "SELECT ordinal, step_id, tool, arguments_json, state FROM agent_task_steps WHERE task_id = ? ORDER BY ordinal",
                (task_id,),
            ).fetchall()
            for step in steps:
                if step["state"] != "RUNNING":
                    continue
                args = json.loads(step["arguments_json"])
                outcome_state = "UNKNOWN_OUTCOME"
                detail = "Process interrupted while step was RUNNING; side effect uncertain. Never auto-replayed."
                effect_state = "UNKNOWN"
                if reconciler is not None:
                    status_code, recon_detail = reconciler(str(step["tool"]), args)
                    if status_code == "verified":
                        outcome_state = "COMPLETED"
                        effect_state = "SUCCEEDED"
                        detail = f"Reconciled via read-only postcondition: {recon_detail}"
                    elif status_code == "not_applied":
                        outcome_state = "FAILED"
                        effect_state = "NOT_STARTED"
                        detail = f"Reconciled as not applied: {recon_detail}"
                if outcome_state == "UNKNOWN_OUTCOME":
                    any_unknown = True
                conn.execute(
                    "UPDATE agent_task_steps SET state = ?, effect_state = ?, evidence = ? WHERE task_id = ? AND ordinal = ?",
                    (outcome_state, effect_state, detail[:500], task_id, step["ordinal"]),
                )
                step_reports.append(
                    {
                        "ordinal": step["ordinal"],
                        "step_id": step["step_id"],
                        "tool": step["tool"],
                        "reconciled_state": outcome_state,
                        "detail": detail,
                    }
                )
            new_task_state = "UNKNOWN_OUTCOME" if any_unknown else ("PAUSED" if step_reports else task["state"])
            if step_reports:
                conn.execute(
                    "UPDATE agent_tasks SET state = ?, version = version + 1, updated_at = datetime('now') WHERE id = ?",
                    (new_task_state, task_id),
                )
        return {"task_id": task_id, "state": new_task_state, "reconciled_steps": step_reports}

    def update_world_state(self, task_id: str, patch: dict[str, Any]) -> None:
        row = self.database.fetch_one("SELECT world_state_json FROM agent_tasks WHERE id = ?", (task_id,))
        current = json.loads(row["world_state_json"] or "{}") if row else {}
        current.update(redact_value(patch))
        self.database.execute(
            "UPDATE agent_tasks SET world_state_json = ?, updated_at = datetime('now') WHERE id = ?",
            (json.dumps(current, ensure_ascii=False)[:8000], task_id),
        )

    def transition(self, task_id: str, state: str, *, reason: str = "", error_code: str = "") -> bool:
        """Atomically move a durable task through the public lifecycle states.

        This is intentionally explicit rather than a permissive string update so
        UI/scheduler integrations cannot accidentally turn an uncertain effect
        into success.  The legacy ``COMPLETED`` alias remains readable, but new
        code should use ``SUCCEEDED`` or ``PARTIAL`` after verification.
        """
        if state not in TASK_STATES:
            raise TaskStateError(f"Unsupported task state '{state}'")
        transitions = {
            "CREATED": {"DISCOVERING", "PLANNING", "AWAITING_APPROVAL", "CANCELLED"},
            "DISCOVERING": {"PLANNING", "WAITING_FOR_USER", "BLOCKED", "CANCELLED"},
            "CLARIFYING": {"PLANNING", "WAITING_FOR_USER", "CANCELLED"},
            "PLANNING": {"AWAITING_APPROVAL", "READY", "WAITING_FOR_USER", "BLOCKED", "FAILED", "CANCELLED"},
            "AWAITING_APPROVAL": {"READY", "WAITING_FOR_USER", "CANCELLED"},
            "READY": {"RUNNING", "WAITING_FOR_DEPENDENCY", "CANCELLED"},
            "WAITING_FOR_DEPENDENCY": {"READY", "BLOCKED", "CANCELLED"},
            "WAITING_FOR_USER": {"READY", "PLANNING", "CANCELLED"},
            "PAUSED": {"READY", "CANCELLED", "UNKNOWN_OUTCOME"},
            "RUNNING": {"VERIFYING", "REPAIRING", "PAUSED", "PARTIAL", "SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED", "UNKNOWN_OUTCOME"},
            "VERIFYING": {"REPAIRING", "SUCCEEDED", "PARTIAL", "FAILED", "WAITING_FOR_USER", "UNKNOWN_OUTCOME"},
            "REPAIRING": {"READY", "RUNNING", "VERIFYING", "PARTIAL", "FAILED", "BLOCKED", "CANCELLED"},
            "REPLANNING": {"AWAITING_APPROVAL", "READY", "BLOCKED", "FAILED", "CANCELLED"},
            "PARTIAL": {"REPAIRING", "VERIFYING", "SUCCEEDED", "CANCELLED"},
            "SUCCEEDED": set(),
            "COMPLETED": set(),
            "FAILED": {"REPAIRING", "REPLANNING"},
            "BLOCKED": {"PLANNING", "READY", "CANCELLED"},
            "UNKNOWN_OUTCOME": {"WAITING_FOR_USER", "VERIFYING", "CANCELLED"},
            "CANCELLED": set(),
        }
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise TaskStateError("Task not found")
            current = str(row["state"])
            if state != current and state not in transitions.get(current, set()):
                raise TaskStateError(f"Cannot transition task from {current} to {state}")
            result = connection.execute(
                """UPDATE agent_tasks SET state=?, paused_reason=?, error_code=?, version=version+1,
                   updated_at=datetime('now') WHERE id=? AND version=?""",
                (state, reason[:500], error_code[:80], task_id, row["version"]),
            )
            if result.rowcount != 1:
                raise TaskStateError("Concurrent task transition conflict")
            connection.execute(
                """INSERT INTO agent_events(worker, tool, status, task_id, event_type, details_json)
                   VALUES('task_store', '', ?, ?, 'task_state_transition', ?)""",
                (state, task_id, json.dumps(redact_value({"from": current, "to": state, "reason": reason[:500]}), ensure_ascii=False)),
            )
            return True

    def record_step_observation(self, task_id: str, ordinal: int, observation: dict[str, Any], *, effect_state: str = "SUCCEEDED", retry_count: int = 0) -> None:
        self.database.execute(
            """UPDATE agent_task_steps SET observation_json = ?, effect_state = ?, retry_count = ?
               WHERE task_id = ? AND ordinal = ?""",
            (
                json.dumps(redact_value(observation), ensure_ascii=False)[:4000],
                effect_state,
                retry_count,
                task_id,
                ordinal,
            ),
        )

    def export_task(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise TaskStateError("Task not found")
        return redact_value(task)

    def dashboard_summary(self) -> dict[str, Any]:
        counts_rows = self.database.fetch_all(
            "SELECT state, COUNT(*) AS count FROM agent_tasks GROUP BY state"
        )
        counts = {str(r["state"]): int(r["count"]) for r in counts_rows}
        return {
            "counts_by_state": counts,
            "recent": self.recent(25),
        }

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
        if state not in {"FAILED", "BLOCKED", "PARTIAL", "CANCELLED", "UNKNOWN_OUTCOME"}:
            raise ValueError("Invalid final task state")
        with self.database.connect() as connection:
            result = connection.execute("""UPDATE agent_tasks SET state=?, updated_at=datetime('now')
                WHERE id=? AND state IN ('RUNNING', 'PAUSED', 'REPLANNING')""", (state, task_id))
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
            "SELECT * FROM agent_task_steps WHERE task_id=? ORDER BY ordinal", (task_id,)
        )
        return task

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM agent_tasks ORDER BY rowid DESC LIMIT ?", (max(1, min(limit, 100)),)
        )
