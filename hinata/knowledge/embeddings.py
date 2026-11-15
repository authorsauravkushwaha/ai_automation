"""Local Ollama embedding adapter. Never pulls or falls back to another model."""
from __future__ import annotations

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
        names = self.llm.list_models(timeout=3)
        for entry in names:
            name, digest = entry.get("name"), entry.get("digest")
            if name in {self.model, f"{self.model}:latest"} and isinstance(digest, str) and digest:
                return f"{name}@{digest}"
        raise OllamaError(f"Local embedding model '{self.model}' is missing or has no digest. Install it explicitly with ollama pull {self.model}.")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.llm.embed_texts(self.model, texts)
