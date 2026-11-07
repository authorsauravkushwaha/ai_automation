"""Persistent, inspectable and bounded local memory."""

from __future__ import annotations

import json
from typing import Any

from friday.database.database import Database


class Memory:
    def __init__(self, database: Database, conversation_limit: int = 1000) -> None:
        self.database = database
        self.conversation_limit = max(100, conversation_limit)

    def add_message(self, role: str, content: str, source: str = "text") -> None:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Invalid conversation role")
        self.database.execute(
            "INSERT INTO conversations(role, content, source) VALUES(?, ?, ?)",
            (role, content, source),
        )
        self.database.execute(
            """DELETE FROM conversations WHERE id NOT IN
               (SELECT id FROM conversations ORDER BY id DESC LIMIT ?)""",
            (self.conversation_limit,),
        )

    def recent_messages(self, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            """SELECT role, content, created_at FROM
               (SELECT id, role, content, created_at FROM conversations ORDER BY id DESC LIMIT ?)
               ORDER BY id""",
            (max(1, min(limit, 50)),),
        )
        return rows

    def start_command(self, original_text: str, source: str, tool: str, arguments: dict) -> int:
        return self.database.execute(
            """INSERT INTO commands(original_text, source, tool, arguments_json)
               VALUES(?, ?, ?, ?)""",
            (original_text, source, tool, json.dumps(arguments, ensure_ascii=False)),
        )

    def finish_command(
        self, command_id: int, tool: str, success: bool, message: str, result: dict[str, Any]
    ) -> None:
        self.database.execute(
            "UPDATE commands SET status = ?, message = ? WHERE id = ?",
            ("completed" if success else "failed", message, command_id),
        )
        # A clear-memory action can intentionally remove its own command row.
        exists = self.database.fetch_one("SELECT 1 AS present FROM commands WHERE id = ?", (command_id,))
        result_command_id = command_id if exists else None
        self.database.execute(
            """INSERT INTO task_results(command_id, tool, success, result_json)
               VALUES(?, ?, ?, ?)""",
            (result_command_id, tool, int(success), json.dumps(result, ensure_ascii=False, default=str)),
        )

    def set_preference(self, key: str, value: str) -> None:
        self.database.execute(
            """INSERT INTO preferences(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
               updated_at = datetime('now')""",
            (key.strip(), value.strip()),
        )

    def preferences(self) -> list[dict[str, Any]]:
        return self.database.fetch_all("SELECT * FROM preferences ORDER BY key")

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conversations = self.database.fetch_all(
            """SELECT id, created_at, content AS text, 'conversation' AS kind
               FROM conversations WHERE content LIKE ? ESCAPE char(92)
               ORDER BY id DESC LIMIT ?""",
            (pattern, limit),
        )
        preferences = self.database.fetch_all(
            """SELECT key || ': ' || value AS text, updated_at AS created_at,
               'preference' AS kind FROM preferences
               WHERE key LIKE ? ESCAPE char(92) OR value LIKE ? ESCAPE char(92) LIMIT ?""",
            (pattern, pattern, limit),
        )
        return (conversations + preferences)[:limit]

    def clear(self, scope: str) -> int:
        tables = {
            "conversations": ["conversations"],
            "commands": ["task_results", "commands"],
            "preferences": ["preferences"],
            "all": ["task_results", "commands", "conversations", "preferences"],
        }
        if scope not in tables:
            raise ValueError("Memory scope must be conversations, commands, preferences, or all")
        deleted = 0
        for table in tables[scope]:
            row = self.database.fetch_one(f"SELECT COUNT(*) AS count FROM {table}")
            deleted += int(row["count"] if row else 0)
            self.database.execute(f"DELETE FROM {table}")
        return deleted
