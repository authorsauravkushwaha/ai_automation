"""Explicitly imported local text knowledge with lexical and semantic retrieval.

Document text is untrusted data, never an instruction to execute.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.database.database import Database
from hinata.knowledge.documents import extract_document
from hinata.knowledge.embeddings import EmbeddingProvider

CHUNK_SIZE = 1200
OVERLAP = 150
MAX_QUERY_TERMS = 32
MAX_FOLDER_FILES = 20
MAX_FOLDER_DIRECTORIES = 500
FOLDER_SCAN_SECONDS = 5.0


class KnowledgeIndex:
    def __init__(self, database: Database, files: FileManager, embeddings: EmbeddingProvider | None = None) -> None:
        self.database = database
        self.files = files
        self.embeddings = embeddings
        with database.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    path TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    path TEXT NOT NULL REFERENCES knowledge_documents(path) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, content TEXT NOT NULL,
                    PRIMARY KEY(path, ordinal)
                );
                CREATE TABLE IF NOT EXISTS knowledge_vectors (
                    path TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    model_id TEXT NOT NULL, vector_json TEXT NOT NULL,
                    PRIMARY KEY(path, ordinal),
                    FOREIGN KEY(path, ordinal) REFERENCES knowledge_chunks(path, ordinal) ON DELETE CASCADE
                );
            """)

    def index(self, value: str, *, semantic: bool = False) -> dict:
        path = self.files.resolve(value)
        if not path.is_file():
            raise AutomationError("Knowledge import requires a file")
        # Text uses configured extensions; PDF and DOCX have bounded extractors.
        before = path.stat()
        text = extract_document(self.files, path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise AutomationError("Document changed while reading; please retry")
        if not text.strip():
            raise AutomationError("Document has no text to index")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunks = [text[i:i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE - OVERLAP)]
        vectors: list[list[float]] = []
        model_id = ""
        if semantic:
            if self.embeddings is None:
                raise AutomationError("No local embedding provider is configured")
            model_id = self.embeddings.identity()
            for offset in range(0, len(chunks), 8):
                vectors.extend(self.embeddings.embed(chunks[offset:offset + 8]))
            if len(vectors) != len(chunks) or len({len(vector) for vector in vectors}) != 1:
                raise AutomationError("Embedding dimensions or count changed during indexing")
            if self.embeddings.identity() != model_id:
                raise AutomationError("Embedding model changed during indexing; retry")
            current = path.stat()
            if (after.st_size, after.st_mtime_ns) != (current.st_size, current.st_mtime_ns):
                raise AutomationError("Document changed while embedding; please retry")
        with self.database.connect() as connection:
            connection.execute("DELETE FROM knowledge_documents WHERE path = ?", (str(path),))
            connection.execute(
                "INSERT INTO knowledge_documents(path, digest, size, mtime_ns) VALUES(?, ?, ?, ?)",
                (str(path), digest, after.st_size, after.st_mtime_ns),
            )
            connection.executemany(
                "INSERT INTO knowledge_chunks(path, ordinal, content) VALUES(?, ?, ?)",
                ((str(path), i + 1, chunk) for i, chunk in enumerate(chunks)),
            )
            if semantic:
                connection.executemany(
                    "INSERT INTO knowledge_vectors(path, ordinal, model_id, vector_json) VALUES(?, ?, ?, ?)",
                    ((str(path), i + 1, model_id, json.dumps(vector, allow_nan=False)) for i, vector in enumerate(vectors)),
                )
        return {"path": str(path), "chunks": len(chunks), "sha256": digest, "embedding_model": model_id or None}

    def sync_folder(self, value: str) -> dict:
        """Explicit bounded sync: index files and remove missing sources only on full success."""
        root = self.files.resolve(value)
        if not root.is_dir():
            raise AutomationError("Knowledge folder sync requires a directory")
        deadline = time.monotonic() + FOLDER_SCAN_SECONDS
        allowed = set(self.files.config.text_extensions) | {".pdf", ".docx"}
        paths: list[Path] = []
        directories = 0
        def on_error(exc: OSError) -> None:
            raise AutomationError(f"Cannot scan knowledge folder: {exc}") from exc
        for directory, dirnames, filenames in os.walk(root, followlinks=False, onerror=on_error):
            directories += 1
            if directories > MAX_FOLDER_DIRECTORIES or time.monotonic() > deadline:
                raise AutomationError("Knowledge folder scan exceeded its limit; no changes were made")
            base = Path(directory)
            dirnames[:] = sorted(name for name in dirnames if not (base / name).is_symlink())
            for name in sorted(filenames):
                candidate = base / name
                if candidate.suffix.lower() not in allowed or candidate.is_symlink():
                    continue
                resolved = self.files.resolve(str(candidate))
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise AutomationError("A folder entry escaped the selected root") from exc
                paths.append(resolved)
                if len(paths) > MAX_FOLDER_FILES or time.monotonic() > deadline:
                    raise AutomationError("Knowledge folder scan exceeded its limit; no changes were made")
        indexed = []
        unchanged = []
        semantic_cleared = []
        failures = []
        for path in paths:
            try:
                metadata = self.database.fetch_one(
                    "SELECT size, mtime_ns FROM knowledge_documents WHERE path=?", (str(path),)
                )
                stat = path.stat()
                if metadata and (metadata["size"], metadata["mtime_ns"]) == (stat.st_size, stat.st_mtime_ns):
                    unchanged.append(str(path))
                    continue
                had_vectors = self.database.fetch_one(
                    "SELECT 1 AS present FROM knowledge_vectors WHERE path=? LIMIT 1", (str(path),)
                ) is not None
                self.index(str(path))
                indexed.append(str(path))
                if had_vectors:
                    semantic_cleared.append(str(path))
            except (AutomationError, OSError) as exc:
                failures.append({"path": str(path), "error": str(exc)})
        removed = []
        # Do not delete anything after a partial scan/import. This is an explicit
        # owner-approved operation and deletes index entries, never source files.
        if not failures:
            rows = self.database.fetch_all("SELECT path FROM knowledge_documents")
            for row in rows:
                path = Path(row["path"])
                try:
                    path.relative_to(root)
                    resolved = self.files.resolve(str(path), must_exist=False)
                    if resolved == path and not path.exists():
                        if self.remove(str(path)):
                            removed.append(str(path))
                except (ValueError, AutomationError, OSError):
                    continue
        return {"indexed": indexed, "unchanged": unchanged, "semantic_cleared": semantic_cleared,
                "removed": removed, "failures": failures}

    def remove(self, value: str) -> bool:
        # A missing file can still be removed from the index. Resolve continues to
        # enforce current workspace restrictions and rejects escaping symlinks.
        path = self.files.resolve(value, must_exist=False)
        with self.database.connect() as connection:
            cursor = connection.execute("DELETE FROM knowledge_documents WHERE path = ?", (str(path),))
            return cursor.rowcount > 0

    def list_documents(self, limit: int = 100) -> list[dict]:
        rows = self.database.fetch_all("""SELECT d.path, d.size, d.mtime_ns,
            (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.path=d.path) AS chunks,
            (SELECT COUNT(*) FROM knowledge_vectors v WHERE v.path=d.path) AS vectors
            FROM knowledge_documents d ORDER BY d.path LIMIT ?""", (max(1, min(limit, 100)),))
        visible = []
        for row in rows:
            try:
                resolved = self.files.resolve(row["path"], must_exist=False)
                if str(resolved) != row["path"]:
                    continue
                stat = resolved.stat()
                state = "current" if (stat.st_size, stat.st_mtime_ns) == (row["size"], row["mtime_ns"]) else "changed"
            except FileNotFoundError:
                state = "missing"
            except (AutomationError, OSError):
                continue
            visible.append({"path": row["path"], "state": state, "chunks": row["chunks"], "vectors": row["vectors"]})
        return visible

    def search(self, query: str, limit: int = 5) -> list[dict]:
        terms = list(dict.fromkeys(re.findall(r"\w+", query.casefold())))[:MAX_QUERY_TERMS]
        if not terms:
            return []
        rows = self.database.fetch_all("""SELECT c.path, c.ordinal, c.content, d.size, d.mtime_ns
            FROM knowledge_chunks c JOIN knowledge_documents d ON d.path = c.path
            ORDER BY c.path, c.ordinal""")
        rows = self._valid_rows(rows)
        results = []
        for row in rows:
            path = row["path"]
            words = set(re.findall(r"\w+", row["content"].casefold()))
            score = sum(term in words for term in terms)
            if score:
                results.append({"path": path, "chunk": row["ordinal"], "excerpt": row["content"], "score": score})
        results.sort(key=lambda item: (-item["score"], item["path"], item["chunk"]))
        return results[:max(1, min(limit, 10))]

    def _valid_rows(self, rows: list[dict]) -> list[dict]:
        valid: dict[str, bool] = {}
        for row in rows:
            path = row["path"]
            if path not in valid:
                try:
                    resolved = self.files.resolve(path)
                    stat = resolved.stat()
                    valid[path] = str(resolved) == path and resolved.is_file() and (stat.st_size, stat.st_mtime_ns) == (row["size"], row["mtime_ns"])
                except (AutomationError, OSError):
                    valid[path] = False
        return [row for row in rows if valid[row["path"]]]

    def semantic_search(self, query: str, limit: int = 5) -> list[dict]:
        if self.embeddings is None:
            raise AutomationError("No local embedding provider is configured")
        model_id = self.embeddings.identity()
        rows = self.database.fetch_all("""SELECT c.path, c.ordinal, c.content, d.size, d.mtime_ns, v.vector_json
            FROM knowledge_chunks c JOIN knowledge_documents d ON d.path=c.path
            JOIN knowledge_vectors v ON v.path=c.path AND v.ordinal=c.ordinal
            WHERE v.model_id=? ORDER BY c.path,c.ordinal""", (model_id,))
        rows = self._valid_rows(rows)
        if not rows:
            return []
        query_vector = self.embeddings.embed([query])[0]
        if self.embeddings.identity() != model_id:
            raise AutomationError("Embedding model changed during search; retry")
        # Check source freshness again after potentially slow local inference.
        rows = self._valid_rows(rows)
        results = []
        for row in rows:
            vector = json.loads(row["vector_json"])
            if len(vector) != len(query_vector):
                raise AutomationError("Stored embedding dimension differs; reindex the document")
            score = sum(a * b for a, b in zip(vector, query_vector, strict=True))
            results.append({"path": row["path"], "chunk": row["ordinal"], "excerpt": row["content"], "score": score})
        results.sort(key=lambda item: (-item["score"], item["path"], item["chunk"]))
        return results[:max(1, min(limit, 10))]
