"""Persistent, inspectable, and partitioned local memory architecture.

Supports:
- Legacy conversation/command/preference APIs (100% backward compatible)
- Five typed memory categories: ``working``, ``episodic``, ``semantic``,
  ``procedural``, and ``project``
- Provenance, confidence, verification status, approval workflow
  (propose / review / approve / edit / merge / supersede / forget)
- Automatic secret detection & redaction before storage or export
- Contradiction detection on normalized durable fact keys
- Semantic + lexical memory retrieval (with vector embeddings when configured)
- Consolidation of repeated episodic/working facts into vetted long-term memory
- Preserving active/unknown task checkpoints when clearing memory.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from hinata.database.database import Database
from hinata.database.timestamps import to_sqlite_utc_or_raise
from hinata.security.redaction import contains_secrets, detect_secrets, redact_text, redact_value


class MemoryPolicyError(RuntimeError):
    """Raised when a memory operation violates sensitivity, scope, or secret policy."""


class Memory:
    MEMORY_TYPES = ("working", "episodic", "semantic", "procedural", "project")

    def __init__(
        self,
        database: Database,
        conversation_limit: int = 1000,
        *,
        embeddings: Any = None,
    ) -> None:
        self.database = database
        self.database.ensure_migrated()
        if isinstance(conversation_limit, bool) or not isinstance(conversation_limit, int) or not 1 <= conversation_limit <= 100000:
            raise ValueError("Conversation limit must be an integer between 1 and 100000")
        self.conversation_limit = conversation_limit
        self.embeddings = embeddings

    def add_message(self, role: str, content: str, source: str = "text") -> None:
        if role not in {"user", "assistant", "system"}:
            raise ValueError("Invalid conversation role")
        safe_content = redact_text(content)
        self.database.execute(
            "INSERT INTO conversations(role, content, source) VALUES(?, ?, ?)",
            (role, safe_content, source),
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
        safe_text = redact_text(original_text)
        safe_args = redact_value(arguments)
        return self.database.execute(
            """INSERT INTO commands(original_text, source, tool, arguments_json)
               VALUES(?, ?, ?, ?)""",
            (safe_text, source, tool, json.dumps(safe_args, ensure_ascii=False)),
        )

    def finish_command(
        self, command_id: int, tool: str, success: bool, message: str, result: dict[str, Any]
    ) -> None:
        safe_msg = redact_text(message)
        safe_res = redact_value(result)
        self.database.execute(
            "UPDATE commands SET status = ?, message = ? WHERE id = ?",
            ("completed" if success else "failed", safe_msg, command_id),
        )
        # A clear-memory action can intentionally remove its own command row.
        exists = self.database.fetch_one("SELECT 1 AS present FROM commands WHERE id = ?", (command_id,))
        result_command_id = command_id if exists else None
        self.database.execute(
            """INSERT INTO task_results(command_id, tool, success, result_json)
               VALUES(?, ?, ?, ?)""",
            (result_command_id, tool, int(success), json.dumps(safe_res, ensure_ascii=False, default=str)),
        )

    def set_preference(self, key: str, value: str) -> None:
        clean_k = key.strip()
        clean_v = value.strip()
        if contains_secrets(f"{clean_k}={clean_v}"):
            raise MemoryPolicyError(
                "Refusing to save potential password, token, PIN, or secret as a durable preference."
            )
        self.database.execute(
            """INSERT INTO preferences(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
               updated_at = datetime('now')""",
            (clean_k, clean_v),
        )
        # Mirror user-confirmed preference into structured semantic memory with provenance.
        self.propose_record(
            memory_type="semantic",
            content=f"{clean_k}: {clean_v}",
            normalized_key=f"pref:{clean_k.casefold()}",
            provenance="user_confirmed_preference",
            confidence=1.0,
            verification_status="user_confirmed",
            approval_status="approved",
            namespace="user",
            scope="preferences",
        )

    def preferences(self) -> list[dict[str, Any]]:
        return self.database.fetch_all("SELECT * FROM preferences ORDER BY key")

    # ------------------------------------------------- structured memory records
    def propose_record(
        self,
        *,
        memory_type: str,
        content: str,
        normalized_key: str = "",
        namespace: str = "user",
        scope: str = "global",
        owner: str = "owner",
        provenance: str = "assistant_inference",
        confidence: float = 0.7,
        verification_status: str = "unverified",
        approval_status: str = "proposed",
        sensitivity: str = "normal",
        task_id: str = "",
        project_id: str = "",
        document_path: str = "",
        expires_at: str | None = None,
        vectorize: bool = False,
    ) -> dict[str, Any]:
        if memory_type not in self.MEMORY_TYPES:
            raise ValueError(f"memory_type must be one of {self.MEMORY_TYPES}")
        cleaned = content.strip()
        if not cleaned:
            raise ValueError("Memory content cannot be empty")
        detected = detect_secrets(cleaned)
        if detected:
            raise MemoryPolicyError(
                f"Refusing to retain sensitive secret material ({', '.join(detected)}) in durable memory."
            )
        norm_key = normalized_key.strip().casefold()
        # Expiry is compared with SQLite's text datetime('now') in retrieval, so it
        # must be stored in exactly that format. A caller-supplied ISO string
        # ("...T...+02:00") otherwise compares as text and can never expire.
        normalized_expiry: str | None = None
        if expires_at is not None and str(expires_at).strip():
            normalized_expiry = to_sqlite_utc_or_raise(expires_at, field="expires_at")
        contradicts_id = ""
        old_id = ""
        if norm_key and memory_type in {"semantic", "project"}:
            existing = self.database.fetch_one(
                """SELECT memory_id, content FROM memory_records
                   WHERE memory_type = ? AND normalized_key = ? AND superseded_by = ''
                   AND approval_status = 'approved' ORDER BY updated_at DESC LIMIT 1""",
                (memory_type, norm_key),
            )
            if existing and str(existing["content"]).strip() != cleaned:
                if provenance == "user_confirmed_preference":
                    # User explicitly updated preference -> supersede previous record cleanly.
                    old_id = str(existing["memory_id"])
                    contradicts_id = ""
                else:
                    contradicts_id = str(existing["memory_id"])
                    approval_status = "contradiction_review"
            elif existing and str(existing["content"]).strip() == cleaned:
                self.database.execute(
                    "UPDATE memory_records SET updated_at=datetime('now'), last_used_at=datetime('now') WHERE memory_id=?",
                    (existing["memory_id"],),
                )
                return {"memory_id": str(existing["memory_id"]), "status": "deduplicated", "contradicts_id": ""}
            else:
                old_id = ""
        else:
            old_id = ""

        embedding_model = ""
        vector_json = ""
        if vectorize and self.embeddings is not None:
            try:
                embedding_model = str(self.embeddings.identity())
                vec = self.embeddings.embed([cleaned])[0]
                vector_json = json.dumps(vec, allow_nan=False)
            except Exception:
                embedding_model = ""
                vector_json = ""

        mem_id = uuid.uuid4().hex[:16]
        self.database.execute(
            """INSERT INTO memory_records(
                memory_id, memory_type, namespace, scope, owner, content, normalized_key,
                provenance, confidence, verification_status, approval_status, sensitivity,
                contradicts_id, task_id, project_id, document_path, expires_at,
                embedding_model, vector_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mem_id,
                memory_type,
                namespace[:64],
                scope[:64],
                owner[:64],
                cleaned[:4000],
                norm_key[:120],
                provenance[:80],
                max(0.0, min(float(confidence), 1.0)),
                verification_status[:40],
                approval_status[:40],
                sensitivity[:40],
                contradicts_id,
                task_id[:64],
                project_id[:64],
                document_path[:512],
                normalized_expiry,
                embedding_model[:160],
                vector_json,
            ),
        )
        if old_id:
            self.database.execute(
                "UPDATE memory_records SET superseded_by = ?, updated_at = datetime('now') WHERE memory_id = ?",
                (mem_id, old_id),
            )
        return {
            "memory_id": mem_id,
            "status": approval_status,
            "contradicts_id": contradicts_id,
        }

    def record_episode(
        self,
        *,
        task_id: str,
        objective: str,
        outcome: str,
        summary: str,
        evidence: str = "",
    ) -> str:
        # ``evidence`` used to be accepted and then dropped on the floor: the
        # caller's proof (a command's output, a file hash, a test result) never
        # reached storage, so an "episode" could not later be audited. Fold it
        # into the record, redacted like everything else that is persisted.
        parts = [f"Objective: {objective}", f"Outcome: {outcome}", summary]
        if evidence.strip():
            parts.append(f"Evidence: {evidence.strip()}")
        safe_summary = redact_text(" | ".join(p for p in parts if p))[:2000]
        res = self.propose_record(
            memory_type="episodic",
            content=safe_summary,
            normalized_key=f"episode:{task_id}",
            namespace="episodes",
            scope="tasks",
            provenance="task_runtime",
            confidence=1.0,
            verification_status="verified" if outcome in {"COMPLETED", "SUCCEEDED"} else "observed",
            approval_status="approved",
            task_id=task_id,
        )
        return str(res["memory_id"])

    def approve_record(self, memory_id: str) -> bool:
        row = self.database.fetch_one("SELECT contradicts_id FROM memory_records WHERE memory_id = ?", (memory_id,))
        if row is None:
            return False
        with self.database.connect() as conn:
            conn.execute(
                """UPDATE memory_records SET approval_status = 'approved',
                   verification_status = 'user_confirmed', updated_at = datetime('now')
                   WHERE memory_id = ?""",
                (memory_id,),
            )
            if row["contradicts_id"]:
                conn.execute(
                    "UPDATE memory_records SET superseded_by = ?, updated_at = datetime('now') WHERE memory_id = ?",
                    (memory_id, row["contradicts_id"]),
                )
        return True

    def edit_record(self, memory_id: str, new_content: str) -> bool:
        cleaned = new_content.strip()
        if not cleaned:
            raise ValueError("Updated content cannot be empty")
        if contains_secrets(cleaned):
            raise MemoryPolicyError("Refusing to store secrets in edited memory")
        with self.database.connect() as conn:
            cur = conn.execute(
                """UPDATE memory_records SET content = ?, vector_json = '', embedding_model = '',
                   updated_at = datetime('now') WHERE memory_id = ?""",
                (cleaned[:4000], memory_id),
            )
            return cur.rowcount == 1

    def merge_records(self, primary_id: str, secondary_id: str, merged_content: str) -> bool:
        if primary_id == secondary_id:
            return False
        if not self.edit_record(primary_id, merged_content):
            return False
        with self.database.connect() as conn:
            cur = conn.execute(
                "UPDATE memory_records SET superseded_by = ?, approval_status = 'merged', updated_at = datetime('now') WHERE memory_id = ?",
                (primary_id, secondary_id),
            )
            return cur.rowcount == 1

    def forget_record(self, memory_id: str) -> bool:
        with self.database.connect() as conn:
            cur = conn.execute("DELETE FROM memory_records WHERE memory_id = ?", (memory_id,))
            return cur.rowcount == 1

    def list_records(
        self,
        *,
        memory_type: str = "",
        namespace: str = "",
        approval_status: str = "",
        include_superseded: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if not include_superseded:
            clauses.append("superseded_by = ''")
        if memory_type:
            clauses.append("memory_type = ?")
            params.append(memory_type)
        if namespace:
            clauses.append("namespace = ?")
            params.append(namespace)
        if approval_status:
            clauses.append("approval_status = ?")
            params.append(approval_status)
        params.append(max(1, min(limit, 200)))
        return self.database.fetch_all(
            f"""SELECT memory_id, memory_type, namespace, scope, owner, content,
                normalized_key, provenance, confidence, verification_status,
                approval_status, sensitivity, contradicts_id, task_id, project_id,
                created_at, updated_at FROM memory_records
                WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?""",
            tuple(params),
        )

    def consolidate(self, min_occurrences: int = 2) -> list[dict[str, Any]]:
        """Promote repeated successful episodic patterns into proposed procedural memories."""
        episodes = self.database.fetch_all(
            """SELECT content, COUNT(*) AS cnt FROM memory_records
               WHERE memory_type = 'episodic' AND verification_status = 'verified'
               GROUP BY content HAVING COUNT(*) >= ? LIMIT 20""",
            (max(2, min_occurrences),),
        )
        promoted: list[dict[str, Any]] = []
        for ep in episodes:
            # Consolidation runs on every maintenance pass, so it must be
            # idempotent: a workflow that already has a promoted record must not
            # be promoted again (that produced a duplicate every single run,
            # because procedural records were not part of propose_record's
            # existing-key check either).
            already = self.database.fetch_one(
                """SELECT memory_id FROM memory_records
                   WHERE memory_type = 'procedural' AND normalized_key = ?
                   AND superseded_by = '' LIMIT 1""",
                ("proc:" + hashlib.sha256(str(ep["content"]).encode("utf-8")).hexdigest()[:16],),
            )
            if already is not None:
                continue
            res = self.propose_record(
                memory_type="procedural",
                content=f"Reusable verified workflow: {ep['content']}",
                # ``hash()`` is salted per process (PYTHONHASHSEED), so the same
                # workflow produced a different key on every launch and the
                # de-duplication this key exists for never happened. A stable
                # digest of the content is the correct identifier.
                normalized_key="proc:" + hashlib.sha256(
                    str(ep["content"]).encode("utf-8")
                ).hexdigest()[:16],
                namespace="procedures",
                scope="workflows",
                provenance="episodic_consolidation",
                confidence=0.85,
                verification_status="consolidated",
                approval_status="proposed",
            )
            promoted.append(res)
        return promoted

    def retrieve_relevant(
        self,
        query: str,
        *,
        namespace_allowlist: set[str] | None = None,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        """Hybrid lexical + optional semantic retrieval over approved structured memories with explanation."""
        terms = [t for t in re.findall(r"\w+", query.casefold()) if len(t) > 2][:16]
        if not terms:
            return []
        rows = self.database.fetch_all(
            """SELECT memory_id, memory_type, namespace, scope, content, provenance,
               confidence, vector_json, embedding_model, created_at
               FROM memory_records
               WHERE approval_status = 'approved' AND superseded_by = ''
               AND (expires_at IS NULL OR expires_at > datetime('now'))
               ORDER BY updated_at DESC LIMIT 200"""
        )
        query_vec: list[float] | None = None
        active_embed_id = ""
        if self.embeddings is not None:
            try:
                active_embed_id = str(self.embeddings.identity())
                query_vec = self.embeddings.embed([query])[0]
            except Exception:
                query_vec = None

        scored: list[dict[str, Any]] = []
        for row in rows:
            if namespace_allowlist is not None and row["namespace"] not in namespace_allowlist:
                continue
            words = set(re.findall(r"\w+", str(row["content"]).casefold()))
            matched_terms = [t for t in terms if t in words]
            lex_score = len(matched_terms) / max(1, len(terms))
            sem_score = 0.0
            if query_vec and row["vector_json"] and row["embedding_model"] == active_embed_id:
                try:
                    vec = json.loads(row["vector_json"])
                    if len(vec) == len(query_vec):
                        sem_score = max(0.0, sum(a * b for a, b in zip(vec, query_vec, strict=True)))
                except (ValueError, TypeError):
                    sem_score = 0.0
            combined = round(0.6 * lex_score + 0.4 * sem_score, 4) if query_vec else round(lex_score, 4)
            if combined <= 0.0:
                continue
            reason = (
                f"Matched terms {matched_terms[:4]} (score={combined})"
                if matched_terms
                else f"Semantic similarity (score={combined})"
            )
            scored.append(
                {
                    "memory_id": row["memory_id"],
                    "memory_type": row["memory_type"],
                    "namespace": row["namespace"],
                    "content": row["content"],
                    "provenance": row["provenance"],
                    "score": combined,
                    "why_retrieved": reason,
                    "created_at": row["created_at"],
                }
            )
        scored.sort(key=lambda item: (-item["score"], item["memory_id"]))
        selected = scored[: max(1, min(limit, 20))]
        for item in selected:
            self.database.execute(
                "UPDATE memory_records SET last_used_at = datetime('now') WHERE memory_id = ?",
                (item["memory_id"],),
            )
        return selected

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
        structured = self.database.fetch_all(
            """SELECT content AS text, updated_at AS created_at,
               memory_type AS kind, provenance FROM memory_records
               WHERE approval_status = 'approved' AND superseded_by = ''
               AND (expires_at IS NULL OR expires_at > datetime('now'))
               AND content LIKE ? ESCAPE char(92)
               ORDER BY updated_at DESC LIMIT ?""",
            (pattern, limit),
        )
        # De-duplicate mirrored preference strings so existing callers receive clean lists.
        seen_pref_texts = {row["text"] for row in preferences}
        filtered_structured = [
            row for row in structured if not (row["kind"] == "semantic" and row["text"] in seen_pref_texts)
        ]
        return (conversations + preferences + filtered_structured)[:limit]

    def clear(self, scope: str) -> int:
        tables: dict[str, list[str]] = {
            "conversations": ["conversations"],
            "commands": ["task_results", "commands"],
            "preferences": ["preferences"],
            "working": [],
            "episodic": [],
            "semantic": [],
            "procedural": [],
            "project": [],
            "all": ["task_results", "commands", "conversations", "preferences", "memory_records"],
        }
        if scope not in tables:
            raise ValueError(
                "Memory scope must be conversations, commands, preferences, working, episodic, semantic, procedural, project, or all"
            )
        if scope in self.MEMORY_TYPES:
            with self.database.connect() as conn:
                cur = conn.execute("DELETE FROM memory_records WHERE memory_type = ?", (scope,))
                return int(cur.rowcount or 0)
        if scope == "preferences":
            self.database.execute("DELETE FROM memory_records WHERE scope = 'preferences'")

        deleted_tasks = 0
        if scope in {"commands", "all"} and self.database.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'agent_tasks'"
        ):
            # Do not remove active or uncertain checkpoints: deletion would obscure whether
            # an in-flight side effect happened. Terminal tasks may be cleared.
            with self.database.connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM agent_tasks WHERE state IN ('COMPLETED', 'SUCCEEDED', 'FAILED', 'CANCELLED')"
                )
                deleted_tasks = cursor.rowcount
        if scope in {"commands", "all"} and self.database.fetch_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_events'"
        ):
            with self.database.connect() as connection:
                cursor = connection.execute("DELETE FROM agent_events WHERE status IN ('COMPLETED', 'FAILED')")
                deleted_tasks += cursor.rowcount
        # Knowledge imports are separate from conversations, but clearing all
        # memory also removes their locally stored document copies and vectors.
        target_tables = list(tables[scope])
        if scope == "all" and self.database.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_documents'"
        ):
            target_tables.insert(0, "knowledge_documents")
            if self.database.fetch_one(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_reembed_queue'"
            ):
                target_tables.insert(0, "knowledge_reembed_queue")
        deleted = deleted_tasks
        for table in target_tables:
            row = self.database.fetch_one(f"SELECT COUNT(*) AS count FROM {table}")
            deleted += int(row["count"] if row else 0)
            self.database.execute(f"DELETE FROM {table}")
        return deleted
