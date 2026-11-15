"""Structured observability, append-only audit trace events, and runtime metrics."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any

from hinata.database.database import Database
from hinata.security.redaction import redact_value


def _compute_trace_hash(
    prev_hash: str,
    *,
    run_id: str,
    task_id: str,
    step_id: str,
    seq: int,
    agent_id: str,
    tool_id: str,
    event_type: str,
    status: str,
    payload_json: str,
) -> str:
    material = "|".join(
        [
            prev_hash,
            run_id,
            task_id,
            step_id,
            str(seq),
            agent_id,
            tool_id,
            event_type,
            status,
            payload_json,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class TraceRecorder:
    """Records structured, secret-redacted, SHA-256 hash-chained audit trace events in SQLite."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.ensure_migrated()
        self._lock = threading.Lock()
        self._sequences: dict[str, int] = {}
        self._last_hash: str | None = None
        self._metrics = {
            "model_calls": 0,
            "model_failures": 0,
            "model_tokens": 0,
            "model_total_latency_ms": 0.0,
            "tool_calls": 0,
            "tool_failures": 0,
            "tool_total_duration_ms": 0.0,
            "tasks_started": 0,
            "tasks_succeeded": 0,
            "tasks_failed": 0,
        }

    def start_run(
        self,
        *,
        user_goal: str,
        source: str,
        autonomy_mode: str = "APPROVED_PLAN",
        task_id: str | None = None,
        model_identity: str = "",
        contract: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        run_id = uuid.uuid4().hex
        trace_id = uuid.uuid4().hex
        with self._lock:
            self._sequences[run_id] = 0
            self._metrics["tasks_started"] += 1
        safe_goal = str(redact_value(user_goal))[:4000]
        self.database.execute(
            """INSERT INTO agent_runs(
                run_id, task_id, trace_id, source, user_goal, autonomy_mode, state,
                model_identity, contract_json, budget_json
            ) VALUES(?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?)""",
            (
                run_id,
                task_id,
                trace_id,
                source,
                safe_goal,
                autonomy_mode,
                model_identity[:200],
                json.dumps(redact_value(contract or {}), ensure_ascii=False),
                json.dumps(redact_value(budget or {}), ensure_ascii=False),
            ),
        )
        self.record_event(
            run_id=run_id,
            task_id=task_id or "",
            correlation_id=trace_id,
            agent_id="orchestrator",
            event_type="run_started",
            status="CREATED",
            model_identity=model_identity,
            payload={"autonomy_mode": autonomy_mode, "source": source},
        )
        return run_id, trace_id

    def update_run(
        self,
        run_id: str,
        *,
        state: str,
        task_id: str | None = None,
        model_identity: str | None = None,
        usage: dict[str, Any] | None = None,
        summary: str | None = None,
        final_evidence: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        fields = ["state = ?", "version = version + 1", "updated_at = datetime('now')"]
        params: list[Any] = [state]
        if task_id is not None:
            fields.append("task_id = ?")
            params.append(task_id)
        if model_identity is not None:
            fields.append("model_identity = ?")
            params.append(model_identity[:200])
        if usage is not None:
            fields.append("usage_json = ?")
            params.append(json.dumps(redact_value(usage), ensure_ascii=False))
        if summary is not None:
            fields.append("summary = ?")
            params.append(str(redact_value(summary))[:2000])
        if final_evidence is not None:
            fields.append("final_evidence_json = ?")
            params.append(json.dumps(redact_value(final_evidence), ensure_ascii=False))
        if error_code is not None:
            fields.append("error_code = ?")
            params.append(error_code[:100])
        params.append(run_id)
        self.database.execute(
            f"UPDATE agent_runs SET {', '.join(fields)} WHERE run_id = ?",
            tuple(params),
        )
        with self._lock:
            if state in {"SUCCEEDED", "COMPLETED"}:
                self._metrics["tasks_succeeded"] += 1
            elif state in {"FAILED", "UNKNOWN_OUTCOME"}:
                self._metrics["tasks_failed"] += 1

    def _next_sequence(self, run_id: str) -> int:
        with self._lock:
            if run_id not in self._sequences:
                row = self.database.fetch_one(
                    "SELECT COALESCE(MAX(sequence), 0) AS max_seq FROM agent_trace_events WHERE run_id = ?",
                    (run_id,),
                )
                self._sequences[run_id] = int(row["max_seq"]) if row else 0
            self._sequences[run_id] += 1
            return self._sequences[run_id]

    def record_event(
        self,
        *,
        run_id: str,
        event_type: str,
        status: str = "",
        task_id: str = "",
        step_id: str = "",
        agent_id: str = "",
        tool_id: str = "",
        correlation_id: str = "",
        model_identity: str = "",
        duration_ms: float = 0.0,
        retry_count: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> int:
        seq = self._next_sequence(run_id) if run_id else 0
        sanitized_payload = json.dumps(redact_value(payload or {}), ensure_ascii=False, default=str)[:4000]
        with self._lock:
            if self._last_hash is None:
                last_row = self.database.fetch_one(
                    "SELECT event_hash FROM agent_trace_events ORDER BY id DESC LIMIT 1"
                )
                self._last_hash = str(last_row["event_hash"]) if last_row and last_row.get("event_hash") else "GENESIS"
            prev_hash = self._last_hash
            event_hash = _compute_trace_hash(
                prev_hash,
                run_id=run_id,
                task_id=task_id,
                step_id=step_id,
                seq=seq,
                agent_id=agent_id,
                tool_id=tool_id,
                event_type=event_type,
                status=status,
                payload_json=sanitized_payload,
            )
            row_id = self.database.execute(
                """INSERT INTO agent_trace_events(
                    run_id, task_id, step_id, sequence, agent_id, tool_id,
                    correlation_id, event_type, status, model_identity,
                    duration_ms, retry_count, payload_json, prev_hash, event_hash
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    task_id,
                    step_id,
                    seq,
                    agent_id,
                    tool_id,
                    correlation_id,
                    event_type,
                    status,
                    model_identity[:200],
                    round(float(duration_ms), 3),
                    int(retry_count),
                    sanitized_payload,
                    prev_hash,
                    event_hash,
                ),
            )
            self._last_hash = event_hash
            return row_id

    def record_model_metric(self, *, latency_ms: float, tokens: int = 0, success: bool = True) -> None:
        with self._lock:
            self._metrics["model_calls"] += 1
            self._metrics["model_total_latency_ms"] += max(0.0, latency_ms)
            self._metrics["model_tokens"] += max(0, tokens)
            if not success:
                self._metrics["model_failures"] += 1

    def record_tool_metric(self, *, duration_ms: float, success: bool = True) -> None:
        with self._lock:
            self._metrics["tool_calls"] += 1
            self._metrics["tool_total_duration_ms"] += max(0.0, duration_ms)
            if not success:
                self._metrics["tool_failures"] += 1

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run = self.database.fetch_one("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,))
        if run is None:
            return None
        run["events"] = self.timeline(run_id)
        return run

    def recent_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM agent_runs ORDER BY rowid DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        )

    def timeline(self, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM agent_trace_events WHERE run_id = ? ORDER BY sequence ASC LIMIT ?",
            (run_id, max(1, min(limit, 500))),
        )

    def task_timeline(self, task_id: str, limit: int = 200) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM agent_trace_events WHERE task_id = ? ORDER BY id ASC LIMIT ?",
            (task_id, max(1, min(limit, 500))),
        )

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._lock:
            m = dict(self._metrics)
        avg_model_ms = (
            round(m["model_total_latency_ms"] / m["model_calls"], 2) if m["model_calls"] else 0.0
        )
        avg_tool_ms = (
            round(m["tool_total_duration_ms"] / m["tool_calls"], 2) if m["tool_calls"] else 0.0
        )
        tokens_per_sec = (
            round(m["model_tokens"] / (m["model_total_latency_ms"] / 1000.0), 2)
            if m["model_total_latency_ms"] > 0 and m["model_tokens"] > 0
            else 0.0
        )
        return {
            **m,
            "avg_model_latency_ms": avg_model_ms,
            "avg_tool_duration_ms": avg_tool_ms,
            "estimated_tokens_per_second": tokens_per_sec,
            "timestamp_epoch": round(time.time(), 3),
        }

    def verify_integrity(self) -> dict[str, Any]:
        """Verify the cryptographic SHA-256 hash chain across all stored trace events."""
        rows = self.database.fetch_all(
            """SELECT id, run_id, task_id, step_id, sequence, agent_id, tool_id,
                      event_type, status, payload_json, prev_hash, event_hash
               FROM agent_trace_events ORDER BY id ASC"""
        )
        expected_prev: str | None = None
        checked = 0
        for row in rows:
            ev_hash = str(row.get("event_hash") or "")
            prev_h = str(row.get("prev_hash") or "")
            if not ev_hash:
                # Pre-v5 legacy rows without hashes do not break chain start.
                expected_prev = None
                continue
            if expected_prev is not None and prev_h != expected_prev:
                return {
                    "intact": False,
                    "checked_events": checked,
                    "broken_at_id": int(row["id"]),
                    "reason": "prev_hash link mismatch (deleted or reordered audit row)",
                }
            recomputed = _compute_trace_hash(
                prev_h,
                run_id=str(row["run_id"]),
                task_id=str(row["task_id"]),
                step_id=str(row["step_id"]),
                seq=int(row["sequence"]),
                agent_id=str(row["agent_id"]),
                tool_id=str(row["tool_id"]),
                event_type=str(row["event_type"]),
                status=str(row["status"]),
                payload_json=str(row["payload_json"]),
            )
            if recomputed != ev_hash:
                return {
                    "intact": False,
                    "checked_events": checked,
                    "broken_at_id": int(row["id"]),
                    "reason": "event_hash digest mismatch (modified audit payload)",
                }
            expected_prev = ev_hash
            checked += 1
        return {
            "intact": True,
            "checked_events": checked,
            "head_hash": expected_prev or "GENESIS",
        }

    def export_audit(self, limit: int = 200) -> dict[str, Any]:
        return redact_value(
            {
                "integrity": self.verify_integrity(),
                "metrics": self.metrics_snapshot(),
                "runs": self.recent_runs(limit),
                "recent_trace_events": self.database.fetch_all(
                    "SELECT * FROM agent_trace_events ORDER BY id DESC LIMIT ?",
                    (max(1, min(limit, 500)),),
                ),
            }
        )

    def clear_audit_with_policy(self, *, actor: str = "owner_pin") -> int:
        """Delete historical terminal audit traces only under explicit policy, and re-anchor the chain."""
        with self._lock:
            with self.database.connect() as conn:
                c1 = conn.execute(
                    "DELETE FROM agent_trace_events WHERE run_id IN (SELECT run_id FROM agent_runs WHERE state IN ('SUCCEEDED', 'COMPLETED', 'FAILED', 'CANCELLED'))"
                )
                c2 = conn.execute(
                    "DELETE FROM agent_runs WHERE state IN ('SUCCEEDED', 'COMPLETED', 'FAILED', 'CANCELLED')"
                )
                deleted = (c1.rowcount or 0) + (c2.rowcount or 0)
            self._last_hash = None
        audit_run = f"audit-clear-{uuid.uuid4().hex[:12]}"
        self.record_event(
            run_id=audit_run,
            agent_id="security_policy",
            event_type="audit_cleared",
            status="COMPLETED",
            payload={"actor": actor, "deleted_records": deleted},
        )
        return deleted
