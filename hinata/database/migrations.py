"""Versioned SQLite schema migrations for HINATA.

All schema evolution runs through ordered, transactional migrations recorded in
``schema_migrations``. Existing legacy databases created before versioned
migrations are cleanly upgraded in-place without losing rows or loosening
verification states.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _m001_baseline_core(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
            content TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'text'
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_id ON conversations(id DESC);

        CREATE TABLE IF NOT EXISTS commands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            original_text TEXT NOT NULL,
            source TEXT NOT NULL,
            tool TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            message TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS task_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            command_id INTEGER,
            tool TEXT NOT NULL,
            success INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            FOREIGN KEY(command_id) REFERENCES commands(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )


def _m002_tasks_and_knowledge(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            objective TEXT NOT NULL,
            source TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS agent_task_steps (
            task_id TEXT NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            tool TEXT NOT NULL,
            arguments_json TEXT NOT NULL,
            state TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(task_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            worker TEXT NOT NULL,
            tool TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            path TEXT PRIMARY KEY,
            digest TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            path TEXT NOT NULL REFERENCES knowledge_documents(path) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            content TEXT NOT NULL,
            PRIMARY KEY(path, ordinal)
        );
        CREATE TABLE IF NOT EXISTS knowledge_vectors (
            path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            model_id TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            PRIMARY KEY(path, ordinal),
            FOREIGN KEY(path, ordinal) REFERENCES knowledge_chunks(path, ordinal) ON DELETE CASCADE
        );
        """
    )

    task_cols = _columns(conn, "agent_tasks")
    if "parent_id" not in task_cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN parent_id TEXT")
    if "revision" not in task_cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    if "user_request" not in task_cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN user_request TEXT NOT NULL DEFAULT ''")
    if "verification_json" not in task_cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN verification_json TEXT NOT NULL DEFAULT '[]'")
    if "verification_method" not in task_cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN verification_method TEXT NOT NULL DEFAULT ''")
        if "attestation" in task_cols:
            conn.execute("UPDATE agent_tasks SET verification_method='human' WHERE attestation <> ''")
    if "objective_evidence" not in task_cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN objective_evidence TEXT NOT NULL DEFAULT ''")
    if "attestation" not in task_cols:
        conn.execute("ALTER TABLE agent_tasks ADD COLUMN attestation TEXT NOT NULL DEFAULT ''")
        # Legacy unverified COMPLETED rows must become VERIFYING rather than trusted complete.
        conn.execute("UPDATE agent_tasks SET state='VERIFYING' WHERE state='COMPLETED'")


def _m003_agentic_runtime_and_observability(conn: sqlite3.Connection) -> None:
    # Extend agent_tasks with versioned optimistic concurrency, plan graph metadata,
    # autonomy mode, owner, lease/heartbeat, budgets, and approval binding.
    task_cols = _columns(conn, "agent_tasks")
    task_additions = [
        ("version", "INTEGER NOT NULL DEFAULT 1"),
        ("owner", "TEXT NOT NULL DEFAULT 'local_owner'"),
        ("autonomy_mode", "TEXT NOT NULL DEFAULT 'APPROVED_PLAN'"),
        ("plan_hash", "TEXT NOT NULL DEFAULT ''"),
        ("approval_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("contract_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("budget_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("world_state_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("model_identity", "TEXT NOT NULL DEFAULT ''"),
        ("lease_owner", "TEXT NOT NULL DEFAULT ''"),
        ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
        ("paused_reason", "TEXT NOT NULL DEFAULT ''"),
        ("error_code", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col, spec in task_additions:
        if col not in task_cols:
            conn.execute(f"ALTER TABLE agent_tasks ADD COLUMN {col} {spec}")

    # Extend agent_task_steps with graph step_id, dependencies, agent_id, risk,
    # idempotency, effect_state, observations, retries, and compensation metadata.
    step_cols = _columns(conn, "agent_task_steps")
    step_additions = [
        ("step_id", "TEXT NOT NULL DEFAULT ''"),
        ("agent_id", "TEXT NOT NULL DEFAULT 'execution'"),
        ("depends_on_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("risk_tier", "TEXT NOT NULL DEFAULT 'low'"),
        ("side_effect", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("idempotency", "TEXT NOT NULL DEFAULT 'non_idempotent'"),
        ("idempotency_key", "TEXT NOT NULL DEFAULT ''"),
        ("effect_state", "TEXT NOT NULL DEFAULT 'NOT_STARTED'"),
        ("observation_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("compensation_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("max_retries", "INTEGER NOT NULL DEFAULT 0"),
        ("timeout_seconds", "REAL NOT NULL DEFAULT 30.0"),
        ("explanation", "TEXT NOT NULL DEFAULT ''"),
    ]
    for col, spec in step_additions:
        if col not in step_cols:
            conn.execute(f"ALTER TABLE agent_task_steps ADD COLUMN {col} {spec}")

    # Backfill stable step_id on any existing rows without one.
    conn.execute(
        "UPDATE agent_task_steps SET step_id = 'step_' || ordinal WHERE step_id = ''"
    )

    # Extend agent_events with rich observability trace columns while preserving existing columns.
    event_cols = _columns(conn, "agent_events")
    event_additions = [
        ("run_id", "TEXT NOT NULL DEFAULT ''"),
        ("task_id", "TEXT NOT NULL DEFAULT ''"),
        ("step_id", "TEXT NOT NULL DEFAULT ''"),
        ("correlation_id", "TEXT NOT NULL DEFAULT ''"),
        ("sequence", "INTEGER NOT NULL DEFAULT 0"),
        ("event_type", "TEXT NOT NULL DEFAULT 'tool_dispatch'"),
        ("model_id", "TEXT NOT NULL DEFAULT ''"),
        ("duration_ms", "REAL NOT NULL DEFAULT 0.0"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("error_code", "TEXT NOT NULL DEFAULT ''"),
        ("details_json", "TEXT NOT NULL DEFAULT '{}'"),
    ]
    for col, spec in event_additions:
        if col not in event_cols:
            conn.execute(f"ALTER TABLE agent_events ADD COLUMN {col} {spec}")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY,
            task_id TEXT REFERENCES agent_tasks(id) ON DELETE SET NULL,
            trace_id TEXT NOT NULL,
            source TEXT NOT NULL,
            user_goal TEXT NOT NULL,
            autonomy_mode TEXT NOT NULL DEFAULT 'APPROVED_PLAN',
            state TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            model_identity TEXT NOT NULL DEFAULT '',
            contract_json TEXT NOT NULL DEFAULT '{}',
            budget_json TEXT NOT NULL DEFAULT '{}',
            usage_json TEXT NOT NULL DEFAULT '{}',
            summary TEXT NOT NULL DEFAULT '',
            final_evidence_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id);

        CREATE TABLE IF NOT EXISTS agent_trace_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT '',
            step_id TEXT NOT NULL DEFAULT '',
            sequence INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            agent_id TEXT NOT NULL DEFAULT '',
            tool_id TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '',
            model_identity TEXT NOT NULL DEFAULT '',
            duration_ms REAL NOT NULL DEFAULT 0.0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_trace_run_seq ON agent_trace_events(run_id, sequence);

        CREATE TABLE IF NOT EXISTS agent_messages (
            message_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL DEFAULT '',
            root_task_id TEXT NOT NULL,
            child_task_id TEXT NOT NULL DEFAULT '',
            correlation_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            message_type TEXT NOT NULL,
            trust_label TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'delivered',
            deadline_epoch REAL,
            content_hash TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_agent_messages_task ON agent_messages(root_task_id);

        CREATE TABLE IF NOT EXISTS tool_idempotency (
            idempotency_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL DEFAULT '',
            step_id TEXT NOT NULL DEFAULT '',
            tool TEXT NOT NULL,
            effect_state TEXT NOT NULL,
            result_summary_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            schedule_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            request_text TEXT NOT NULL,
            autonomy_mode TEXT NOT NULL DEFAULT 'SUGGEST_ONLY',
            schedule_kind TEXT NOT NULL CHECK(schedule_kind IN ('once', 'interval', 'condition')),
            next_run_epoch REAL NOT NULL,
            interval_seconds INTEGER NOT NULL DEFAULT 0,
            expires_epoch REAL,
            enabled INTEGER NOT NULL DEFAULT 1,
            requires_online INTEGER NOT NULL DEFAULT 0,
            max_runs INTEGER NOT NULL DEFAULT 1,
            run_count INTEGER NOT NULL DEFAULT 0,
            last_task_id TEXT NOT NULL DEFAULT '',
            last_status TEXT NOT NULL DEFAULT 'pending',
            budget_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )


def _m004_memory_rag_and_workspaces(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_records (
            memory_id TEXT PRIMARY KEY,
            memory_type TEXT NOT NULL CHECK(memory_type IN ('working', 'episodic', 'semantic', 'procedural', 'project')),
            namespace TEXT NOT NULL DEFAULT 'user',
            scope TEXT NOT NULL DEFAULT 'global',
            owner TEXT NOT NULL DEFAULT 'owner',
            content TEXT NOT NULL,
            normalized_key TEXT NOT NULL DEFAULT '',
            provenance TEXT NOT NULL DEFAULT 'user',
            confidence REAL NOT NULL DEFAULT 1.0,
            verification_status TEXT NOT NULL DEFAULT 'unverified',
            approval_status TEXT NOT NULL DEFAULT 'approved',
            sensitivity TEXT NOT NULL DEFAULT 'normal',
            superseded_by TEXT NOT NULL DEFAULT '',
            contradicts_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            document_path TEXT NOT NULL DEFAULT '',
            expires_at TEXT,
            embedding_model TEXT NOT NULL DEFAULT '',
            vector_json TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_memory_type_scope ON memory_records(memory_type, scope, approval_status);

        CREATE TABLE IF NOT EXISTS coding_workspaces (
            workspace_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL UNIQUE,
            approved INTEGER NOT NULL DEFAULT 1,
            git_enabled INTEGER NOT NULL DEFAULT 0,
            test_profile TEXT NOT NULL DEFAULT 'python_unittest',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS coding_patches (
            patch_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES coding_workspaces(workspace_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL,
            diff_text TEXT NOT NULL,
            files_json TEXT NOT NULL,
            backup_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'PROPOSED',
            review_json TEXT NOT NULL DEFAULT '{}',
            test_report_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS knowledge_reembed_queue (
            path TEXT PRIMARY KEY,
            reason TEXT NOT NULL DEFAULT 'changed',
            queued_at TEXT NOT NULL DEFAULT (datetime('now')),
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS offline_assets (
            asset_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            version TEXT NOT NULL DEFAULT '',
            local_path TEXT NOT NULL DEFAULT '',
            expected_sha256 TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            license TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unknown',
            verified_at TEXT NOT NULL DEFAULT ''
        );
        """
    )

    chunk_cols = _columns(conn, "knowledge_chunks")
    for col, spec in (
        ("section", "TEXT NOT NULL DEFAULT ''"),
        ("page_number", "INTEGER NOT NULL DEFAULT 0"),
        ("line_start", "INTEGER NOT NULL DEFAULT 0"),
        ("line_end", "INTEGER NOT NULL DEFAULT 0"),
        ("token_estimate", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in chunk_cols:
            conn.execute(f"ALTER TABLE knowledge_chunks ADD COLUMN {col} {spec}")

    doc_cols = _columns(conn, "knowledge_documents")
    for col, spec in (
        ("doc_type", "TEXT NOT NULL DEFAULT 'text'"),
        ("project_id", "TEXT NOT NULL DEFAULT ''"),
        ("indexed_at", "TEXT NOT NULL DEFAULT ''"),
        ("semantic_requested", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in doc_cols:
            conn.execute(f"ALTER TABLE knowledge_documents ADD COLUMN {col} {spec}")


def _m005_tamper_evident_traces_and_playbooks(conn: sqlite3.Connection) -> None:
    """Migration 5: Hash-chained trace ledger and reusable procedural playbooks."""
    trace_cols = _columns(conn, "agent_trace_events")
    for col, spec in (
        ("prev_hash", "TEXT NOT NULL DEFAULT ''"),
        ("event_hash", "TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in trace_cols:
            conn.execute(f"ALTER TABLE agent_trace_events ADD COLUMN {col} {spec}")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS procedural_playbooks (
            playbook_id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            steps_json TEXT NOT NULL,
            source_task_id TEXT NOT NULL DEFAULT '',
            plan_hash TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            use_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )


def _m006_agent_reports_and_project_records(conn: sqlite3.Connection) -> None:
    """Migration 6: durable specialist reports and truthful project lifecycle records."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_subtask_reports (
            report_id TEXT PRIMARY KEY,
            root_task_id TEXT NOT NULL,
            child_task_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            status TEXT NOT NULL,
            success INTEGER NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            structured_data_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            proposed_actions_json TEXT NOT NULL DEFAULT '[]',
            limitations_json TEXT NOT NULL DEFAULT '[]',
            model_identity TEXT NOT NULL DEFAULT '',
            usage_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_agent_subtask_reports_task
            ON agent_subtask_reports(root_task_id, created_at);

        CREATE TABLE IF NOT EXISTS project_records (
            project_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            root_path TEXT NOT NULL UNIQUE,
            project_type TEXT NOT NULL DEFAULT 'unknown',
            stack_json TEXT NOT NULL DEFAULT '{}',
            requirements_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'DISCOVERING',
            verification_json TEXT NOT NULL DEFAULT '{}',
            file_inventory_json TEXT NOT NULL DEFAULT '[]',
            known_issues_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_project_records_status ON project_records(status);

        CREATE TABLE IF NOT EXISTS project_iterations (
            iteration_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES project_records(project_id) ON DELETE CASCADE,
            phase TEXT NOT NULL,
            status TEXT NOT NULL,
            changed_files_json TEXT NOT NULL DEFAULT '[]',
            checks_json TEXT NOT NULL DEFAULT '{}',
            diagnostics_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_project_iterations_project
            ON project_iterations(project_id, created_at);
        """
    )


def _m007_resumable_plan_conditions(conn: sqlite3.Connection) -> None:
    """Persist branch conditions so an approved DAG can be restored exactly."""
    if _table_exists(conn, "agent_task_steps"):
        step_cols = _columns(conn, "agent_task_steps")
        if "condition" not in step_cols:
            conn.execute("ALTER TABLE agent_task_steps ADD COLUMN condition TEXT NOT NULL DEFAULT ''")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "001_baseline_core", _m001_baseline_core),
    Migration(2, "002_tasks_and_knowledge", _m002_tasks_and_knowledge),
    Migration(3, "003_agentic_runtime_and_observability", _m003_agentic_runtime_and_observability),
    Migration(4, "004_memory_rag_and_workspaces", _m004_memory_rag_and_workspaces),
    Migration(5, "005_tamper_evident_traces_and_playbooks", _m005_tamper_evident_traces_and_playbooks),
    Migration(6, "006_agent_reports_and_project_records", _m006_agent_reports_and_project_records),
    Migration(7, "007_resumable_plan_conditions", _m007_resumable_plan_conditions),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def apply_migrations(conn: sqlite3.Connection, *, upto_version: int | None = None) -> int:
    """Apply any unapplied migrations transactionally and return the active schema version."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    applied = {
        int(row["version"])
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    target = LATEST_SCHEMA_VERSION if upto_version is None else upto_version
    last_applied = max(applied) if applied else 0
    for migration in MIGRATIONS:
        if migration.version > target:
            break
        if migration.version in applied:
            continue
        migration.apply(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version, name) VALUES(?, ?)",
            (migration.version, migration.name),
        )
        last_applied = max(last_applied, migration.version)
    return last_applied
