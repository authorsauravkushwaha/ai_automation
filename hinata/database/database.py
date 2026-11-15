"""Small SQLite wrapper; one short-lived connection per operation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hinata.database.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from hinata.security.local_protection import LocalVault
from hinata.security.redaction import redact_value

SCHEMA = """
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


class Database:
    def __init__(self, path: str | Path, *, vault: LocalVault | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.vault = vault
        self.schema_version = LATEST_SCHEMA_VERSION
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            self.schema_version = apply_migrations(connection, upto_version=1)

    def ensure_migrated(self) -> int:
        with self.connect() as connection:
            self.schema_version = apply_migrations(connection)
            return self.schema_version

    def migrations(self) -> list[dict[str, Any]]:
        return self.fetch_all("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(query, parameters)
            return int(cursor.lastrowid or 0)

    def fetch_all(self, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def fetch_one(self, query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(query, parameters).fetchone()
            return dict(row) if row else None

    def get_setting(self, key: str) -> str | None:
        row = self.fetch_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            """INSERT INTO settings(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
               updated_at = datetime('now')""",
            (key, value),
        )

    def export_memory(self) -> dict[str, Any]:
        """Export user memory with automatic secret redaction, excluding settings and PIN material."""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "conversations": self.fetch_all(
                "SELECT id, created_at, role, content, source FROM conversations ORDER BY id"
            ),
            "commands": self.fetch_all(
                """SELECT id, created_at, original_text, source, tool, arguments_json,
                   status, message FROM commands ORDER BY id"""
            ),
            "task_results": self.fetch_all(
                "SELECT id, created_at, command_id, tool, success, result_json FROM task_results"
            ),
            "preferences": self.fetch_all("SELECT * FROM preferences ORDER BY key"),
            "memory_records": self.fetch_all(
                """SELECT memory_id, memory_type, namespace, scope, owner, content,
                   normalized_key, provenance, confidence, verification_status,
                   approval_status, sensitivity, task_id, project_id, created_at, updated_at
                   FROM memory_records ORDER BY created_at"""
            ),
            "tasks": self.fetch_all(
                """SELECT id, objective, source, state, autonomy_mode, verification_method,
                   created_at, updated_at FROM agent_tasks ORDER BY rowid"""
            ),
        }
        return redact_value(payload)

    def export_json(self, path: str | Path, *, passphrase: str | None = None) -> Path:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = self.export_memory()
        if passphrase:
            vault = self.vault or LocalVault(self.path.parent)
            return vault.export_encrypted_json(payload, passphrase, destination)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(destination)
        return destination

    def backup_to(self, destination: str | Path, *, passphrase: str | None = None) -> Path:
        """Create a consistent SQLite backup or passphrase-encrypted archive."""
        dest_path = Path(destination).expanduser().resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        if passphrase:
            return self.export_json(dest_path, passphrase=passphrase)
        with self.connect() as source_conn:
            target_conn = sqlite3.connect(dest_path)
            try:
                source_conn.backup(target_conn)
            finally:
                target_conn.close()
        return dest_path
