"""Explicitly imported local text knowledge with lexical and semantic retrieval.

Document text is untrusted data, never an instruction to execute.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.database.database import Database
from hinata.knowledge.documents import extract_document
from hinata.knowledge.embeddings import EmbeddingProvider

CHUNK_SIZE = 1200
OVERLAP = 150
CHUNK_TOKEN_TARGET = 280
OVERLAP_TOKENS = 40
MAX_QUERY_TERMS = 32
MAX_FOLDER_FILES = 20
MAX_FOLDER_DIRECTORIES = 500
FOLDER_SCAN_SECONDS = 5.0
DEFAULT_MIN_RELEVANCE = 0.05


def _estimate_tokens(text: str) -> int:
    return max(len(re.findall(r"\S+", text)), (len(text) + 3) // 4)


def _structure_aware_chunks(text: str, *, max_chars: int = CHUNK_SIZE, overlap_chars: int = OVERLAP) -> list[dict]:
    """Split text along page markers, headings, and paragraphs with line/section/page citations."""
    lines = text.splitlines()
    chunks: list[dict] = []
    current_lines: list[str] = []
    current_chars = 0
    current_section = ""
    current_page = 0
    chunk_line_start = 1

    def _flush(end_line: int) -> None:
        nonlocal current_lines, current_chars, chunk_line_start
        body = "\n".join(current_lines).strip()
        if body:
            chunks.append(
                {
                    "content": body,
                    "section": current_section[:120],
                    "page_number": current_page,
                    "line_start": chunk_line_start,
                    "line_end": max(chunk_line_start, end_line),
                    "token_estimate": _estimate_tokens(body),
                }
            )
        # Keep trailing overlap lines for continuity.
        overlap_buf: list[str] = []
        ov_chars = 0
        for ln in reversed(current_lines):
            if ov_chars + len(ln) + 1 > overlap_chars:
                break
            overlap_buf.append(ln)
            ov_chars += len(ln) + 1
        current_lines = list(reversed(overlap_buf))
        current_chars = sum(len(ln) + 1 for ln in current_lines)
        chunk_line_start = max(1, end_line - len(current_lines) + 1)

    for line_no, raw_line in enumerate(lines, 1):
        page_match = re.fullmatch(r"\[\[PAGE:(\d+)\]\]", raw_line.strip())
        if page_match:
            if current_lines:
                _flush(line_no - 1)
                current_lines = []
                current_chars = 0
                chunk_line_start = line_no + 1
            current_page = int(page_match.group(1))
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", raw_line.strip())
        if heading_match:
            if current_chars >= max_chars // 2:
                _flush(line_no - 1)
            current_section = heading_match.group(2).strip()

        if len(raw_line) > max_chars:
            # Long unbroken line fallback (keeps exact CHUNK_SIZE bounds).
            if current_lines:
                _flush(line_no - 1)
                current_lines = []
                current_chars = 0
            step = max(1, max_chars - overlap_chars)
            for offset in range(0, len(raw_line), step):
                piece = raw_line[offset : offset + max_chars]
                chunks.append(
                    {
                        "content": piece,
                        "section": current_section[:120],
                        "page_number": current_page,
                        "line_start": line_no,
                        "line_end": line_no,
                        "token_estimate": _estimate_tokens(piece),
                    }
                )
            chunk_line_start = line_no + 1
            continue

        if current_chars + len(raw_line) + 1 > max_chars and current_lines:
            _flush(line_no - 1)
        if not current_lines:
            chunk_line_start = line_no
        current_lines.append(raw_line)
        current_chars += len(raw_line) + 1

    if current_lines:
        _flush(len(lines))

    return chunks


def verify_claim_citations(answer: str, matches: list[dict]) -> dict:
    """Claim-level citation verifier: ensure every cited claim shares substantive lexical support."""
    citations = re.findall(r"\[(\d+)\]", answer)
    if not citations:
        return {"valid": False, "reason": "No source citations present", "unsupported_claims": []}
    if any(not 1 <= int(num) <= len(matches) for num in citations):
        return {"valid": False, "reason": "Out-of-range citation index", "unsupported_claims": []}

    stopwords = {
        "the", "and", "for", "that", "this", "with", "from", "are", "was", "were", "have",
        "has", "had", "not", "but", "what", "when", "where", "who", "which", "into", "only",
        "local", "source", "sources", "answer", "according", "passage", "document",
    }
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", answer) if s.strip()]
    unsupported: list[str] = []
    for sent in sentences:
        refs = [int(n) for n in re.findall(r"\[(\d+)\]", sent)]
        if not refs:
            continue
        clean_sent = re.sub(r"\[\d+\]", "", sent)
        claim_words = {
            w for w in re.findall(r"[a-zA-Z0-9]{3,}", clean_sent.casefold()) if w not in stopwords
        }
        if not claim_words:
            continue
        ref_words: set[str] = set()
        for r in refs:
            excerpt = str(matches[r - 1].get("excerpt", ""))
            ref_words.update(re.findall(r"[a-zA-Z0-9]{3,}", excerpt.casefold()))
        overlap = claim_words & ref_words
        if not overlap:
            unsupported.append(sent[:200])
    if unsupported:
        return {
            "valid": False,
            "reason": "Cited passage does not lexically support claim",
            "unsupported_claims": unsupported,
        }
    return {"valid": True, "reason": "ok", "unsupported_claims": []}


class KnowledgeIndex:
    def __init__(self, database: Database, files: FileManager, embeddings: EmbeddingProvider | None = None) -> None:
        self.database = database
        self.database.ensure_migrated()
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
        structured_chunks = _structure_aware_chunks(text, max_chars=CHUNK_SIZE, overlap_chars=OVERLAP)
        chunks = [c["content"] for c in structured_chunks]
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
        doc_type = path.suffix.lower().lstrip(".") or "text"
        with self.database.connect() as connection:
            connection.execute("DELETE FROM knowledge_documents WHERE path = ?", (str(path),))
            connection.execute(
                """INSERT INTO knowledge_documents(path, digest, size, mtime_ns, doc_type, indexed_at, semantic_requested)
                   VALUES(?, ?, ?, ?, ?, datetime('now'), ?)""",
                (str(path), digest, after.st_size, after.st_mtime_ns, doc_type, int(semantic)),
            )
            connection.executemany(
                """INSERT INTO knowledge_chunks(path, ordinal, content, section, page_number, line_start, line_end, token_estimate)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        str(path),
                        i + 1,
                        c["content"],
                        c["section"],
                        c["page_number"],
                        c["line_start"],
                        c["line_end"],
                        c["token_estimate"],
                    )
                    for i, c in enumerate(structured_chunks)
                ),
            )
            if semantic:
                connection.executemany(
                    "INSERT INTO knowledge_vectors(path, ordinal, model_id, vector_json) VALUES(?, ?, ?, ?)",
                    ((str(path), i + 1, model_id, json.dumps(vector, allow_nan=False)) for i, vector in enumerate(vectors)),
                )
                connection.execute("DELETE FROM knowledge_reembed_queue WHERE path = ?", (str(path),))
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
                    "SELECT digest, size, mtime_ns FROM knowledge_documents WHERE path=?", (str(path),)
                )
                if metadata:
                    current_text = extract_document(self.files, path)
                    current_digest = hashlib.sha256(current_text.encode("utf-8")).hexdigest()
                    if metadata["digest"] == current_digest:
                        unchanged.append(str(path))
                        continue
                had_vectors = self.database.fetch_one(
                    "SELECT 1 AS present FROM knowledge_vectors WHERE path=? LIMIT 1", (str(path),)
                ) is not None
                self.index(str(path))
                indexed.append(str(path))
                if had_vectors:
                    semantic_cleared.append(str(path))
                    self.database.execute(
                        """INSERT INTO knowledge_reembed_queue(path, reason) VALUES(?, 'folder_sync_changed')
                           ON CONFLICT(path) DO UPDATE SET reason='folder_sync_changed', queued_at=datetime('now')""",
                        (str(path),),
                    )
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
        rows = self.database.fetch_all("""SELECT c.path, c.ordinal, c.content, c.section, c.page_number,
            c.line_start, c.line_end, d.digest, d.size, d.mtime_ns
            FROM knowledge_chunks c JOIN knowledge_documents d ON d.path = c.path
            ORDER BY c.path, c.ordinal""")
        rows = self._valid_rows(rows)
        results = []
        for row in rows:
            path = row["path"]
            words = set(re.findall(r"\w+", row["content"].casefold()))
            score = sum(term in words for term in terms)
            if score:
                results.append(
                    {
                        "path": path,
                        "chunk": row["ordinal"],
                        "excerpt": row["content"],
                        "score": score,
                        "section": row.get("section", ""),
                        "page": row.get("page_number", 0),
                        "line_start": row.get("line_start", 0),
                        "line_end": row.get("line_end", 0),
                    }
                )
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
                    is_fresh = str(resolved) == path and resolved.is_file() and (stat.st_size, stat.st_mtime_ns) == (row["size"], row["mtime_ns"])
                    if is_fresh and row.get("digest"):
                        current_text = extract_document(self.files, resolved)
                        is_fresh = hashlib.sha256(current_text.encode("utf-8")).hexdigest() == row["digest"]
                    valid[path] = is_fresh
                except (AutomationError, OSError):
                    valid[path] = False
        return [row for row in rows if valid[row["path"]]]

    def semantic_search(self, query: str, limit: int = 5) -> list[dict]:
        if self.embeddings is None:
            raise AutomationError("No local embedding provider is configured")
        model_id = self.embeddings.identity()
        rows = self.database.fetch_all("""SELECT c.path, c.ordinal, c.content, c.section, c.page_number,
            c.line_start, c.line_end, d.digest, d.size, d.mtime_ns, v.vector_json
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
            results.append(
                {
                    "path": row["path"],
                    "chunk": row["ordinal"],
                    "excerpt": row["content"],
                    "score": score,
                    "section": row.get("section", ""),
                    "page": row.get("page_number", 0),
                    "line_start": row.get("line_start", 0),
                    "line_end": row.get("line_end", 0),
                }
            )
        results.sort(key=lambda item: (-item["score"], item["path"], item["chunk"]))
        return results[:max(1, min(limit, 10))]

    def hybrid_search(
        self,
        query: str,
        limit: int = 5,
        *,
        lexical_weight: float = 0.45,
        semantic_weight: float = 0.55,
        min_score: float = DEFAULT_MIN_RELEVANCE,
    ) -> list[dict]:
        """Calibrated hybrid search combining normalized lexical term overlap and cosine similarity."""
        lex_hits = self.search(query, limit=10)
        sem_hits: list[dict] = []
        if self.embeddings is not None:
            try:
                sem_hits = self.semantic_search(query, limit=10)
            except Exception:
                sem_hits = []
        max_lex = max((float(h["score"]) for h in lex_hits), default=1.0) or 1.0
        merged: dict[tuple[str, int], dict] = {}
        for h in lex_hits:
            key = (h["path"], h["chunk"])
            merged[key] = {**h, "lexical_norm": float(h["score"]) / max_lex, "semantic_norm": 0.0}
        for h in sem_hits:
            key = (h["path"], h["chunk"])
            sem_norm = max(0.0, float(h["score"]))
            if key in merged:
                merged[key]["semantic_norm"] = sem_norm
            else:
                merged[key] = {**h, "lexical_norm": 0.0, "semantic_norm": sem_norm}
        ranked: list[dict] = []
        for item in merged.values():
            if sem_hits:
                score = round(lexical_weight * item["lexical_norm"] + semantic_weight * item["semantic_norm"], 4)
            else:
                score = round(item["lexical_norm"], 4)
            if score >= min_score:
                ranked.append({**item, "score": score})
        ranked.sort(key=lambda x: (-x["score"], x["path"], x["chunk"]))
        return ranked[: max(1, min(limit, 10))]

    def reembed_queue(self) -> list[dict]:
        return self.database.fetch_all("SELECT * FROM knowledge_reembed_queue ORDER BY queued_at ASC")

    def process_reembed_queue(self, max_items: int = 5) -> dict:
        rows = self.reembed_queue()[: max(1, min(max_items, 20))]
        completed: list[str] = []
        failed: list[dict] = []
        for row in rows:
            path = str(row["path"])
            try:
                self.index(path, semantic=True)
                completed.append(path)
            except Exception as exc:
                self.database.execute(
                    "UPDATE knowledge_reembed_queue SET attempts = attempts + 1, last_error = ? WHERE path = ?",
                    (str(exc)[:300], path),
                )
                failed.append({"path": path, "error": str(exc)[:300]})
        return {"reembedded": completed, "failed": failed, "remaining": len(self.reembed_queue())}


class ThrottledKnowledgeWatcher:
    """Explicit opt-in folder watcher that periodically triggers bounded lexical syncs."""

    def __init__(self, knowledge: KnowledgeIndex, *, min_interval_seconds: float = 30.0) -> None:
        self.knowledge = knowledge
        self.min_interval_seconds = max(5.0, min_interval_seconds)
        self._watched_folders: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_reports: dict[str, dict] = {}

    def register_folder(self, folder_path: str) -> str:
        resolved = self.knowledge.files.resolve(folder_path)
        if not resolved.is_dir():
            raise AutomationError("Watched knowledge root must be an existing directory")
        canon = str(resolved)
        with self._lock:
            if canon not in self._watched_folders:
                self._watched_folders.append(canon)
        return canon

    def watched_folders(self) -> list[str]:
        with self._lock:
            return list(self._watched_folders)

    def poll_once(self) -> dict[str, dict]:
        reports: dict[str, dict] = {}
        for folder in self.watched_folders():
            try:
                reports[folder] = self.knowledge.sync_folder(folder)
            except Exception as exc:
                reports[folder] = {"error": str(exc)[:300]}
        with self._lock:
            self._last_reports.update(reports)
        return reports

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="hinata-knowledge-watcher", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            if self._stop.wait(self.min_interval_seconds):
                break
