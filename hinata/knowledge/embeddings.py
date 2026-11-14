"""Local Ollama embedding adapter. Never pulls or falls back to another model."""
from __future__ import annotations

import math
from typing import Protocol

from hinata.core.llm_manager import LLMManager, OllamaError


class EmbeddingProvider(Protocol):
    def identity(self) -> str: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbeddings:
    def __init__(self, llm: LLMManager, model: str) -> None:
        self.llm = llm
        self.model = model

    def identity(self) -> str:
        if not self.llm.config.enabled:
            raise OllamaError("Ollama is disabled; semantic retrieval needs a local embedding model")
        if not self.model:
            raise OllamaError("Configure ollama.embedding_model before semantic indexing")
        self.llm.ensure_connected()
        result = self.llm._request("/api/tags", timeout=3)
        names = result.get("models", [])
        for entry in names if isinstance(names, list) else []:
            if not isinstance(entry, dict):
                continue
            name, digest = entry.get("name"), entry.get("digest")
            if name in {self.model, f"{self.model}:latest"} and isinstance(digest, str) and digest:
                return f"{name}@{digest}"
        raise OllamaError(f"Local embedding model '{self.model}' is missing or has no digest. Install it explicitly with ollama pull {self.model}.")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not 1 <= len(texts) <= 8:
            raise ValueError("Embedding batch must contain 1..8 texts")
        result = self.llm._request("/api/embed", {
            "model": self.model, "input": texts, "truncate": False, "keep_alive": 0,
        }, timeout=self.llm.config.timeout_seconds)
        vectors = result.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaError("Embedding model returned an invalid number of vectors")
        validated = []
        for vector in vectors:
            if not isinstance(vector, list) or not 1 <= len(vector) <= 8192:
                raise OllamaError("Embedding model returned an invalid vector")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                raise OllamaError("Embedding model returned a non-finite vector")
            norm = math.sqrt(sum(v * v for v in vector))
            if not math.isfinite(norm) or norm == 0:
                raise OllamaError("Embedding model returned a zero or invalid vector")
            validated.append([float(v / norm) for v in vector])
        if len({len(v) for v in validated}) != 1:
            raise OllamaError("Embedding dimensions do not match")
        return validated
