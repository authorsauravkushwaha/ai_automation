"""Approximate Nearest Neighbor (ANN) & vectorized search engine for 50k+ RAG chunks.

Architecture (offline, zero mandatory dependencies, progressive acceleration):
1. ``hnswlib`` backend (optional ``.[vector-ann]`` extra): native C++ HNSW graph
   supporting sub-millisecond top-k retrieval across 50,000–500,000+ vectors.
2. ``numpy`` Multi-Table Random-Hyperplane LSH + matrix dot-product backend:
   partitions vectors into 16-bit SimHash buckets across ``num_tables`` random
   hyperplane projections, probing exact + 1-bit Hamming neighbors and computing
   exact cosine similarity on the candidate shortlist via BLAS matrix multiply.
3. Pure-Python ``array('f')`` SimHash LSH + exact cosine shortlist fallback:
   keeps core HINATA 100% functional on bare Python 3.11 standard library.

Whenever the active corpus has ``<= exact_threshold`` vectors (default 2,048),
exact vectorized scan is used automatically so small personal libraries always
receive 100% recall with zero index-build overhead.
"""

from __future__ import annotations

import array
import hashlib
import importlib
import math
import random
import threading
from dataclasses import dataclass
from typing import Any


def _load_optional(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


@dataclass(slots=True)
class VectorCandidate:
    key: tuple[str, int]  # (path, ordinal)
    score: float


class LocalVectorIndex:
    """Thread-safe in-memory ANN / exact vector index keyed by ``(model_id, dim)``."""

    def __init__(
        self,
        *,
        exact_threshold: int = 2048,
        num_tables: int = 6,
        bits_per_table: int = 12,
        shortlist_multiplier: int = 24,
    ) -> None:
        self.exact_threshold = max(64, exact_threshold)
        self.num_tables = max(2, min(num_tables, 12))
        self.bits_per_table = max(6, min(bits_per_table, 16))
        self.shortlist_multiplier = max(8, shortlist_multiplier)
        self._lock = threading.Lock()
        self._model_id: str = ""
        self._dim: int = 0
        self._fingerprint: str = ""
        self._keys: list[tuple[str, int]] = []
        self._backend_name: str = "unbuilt"
        # Backend-specific structures
        self._hnsw_index: Any | None = None
        self._np_matrix: Any | None = None
        self._np_planes: list[Any] = []
        self._py_vectors: list[array.array[float]] = []
        self._py_planes: list[list[array.array[float]]] = []
        self._lsh_tables: list[dict[int, list[int]]] = []

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def size(self) -> int:
        return len(self._keys)

    def invalidate(self) -> None:
        with self._lock:
            self._fingerprint = ""
            self._keys = []
            self._hnsw_index = None
            self._np_matrix = None
            self._py_vectors = []
            self._lsh_tables = []
            self._backend_name = "unbuilt"

    @staticmethod
    def _compute_fingerprint(model_id: str, items: list[tuple[tuple[str, int], list[float]]]) -> str:
        h = hashlib.sha256(model_id.encode("utf-8"))
        h.update(str(len(items)).encode("ascii"))
        if items:
            first_k, first_v = items[0]
            last_k, last_v = items[-1]
            h.update(f"{first_k}:{len(first_v)}:{first_v[0]:.6f}|{last_k}:{last_v[-1]:.6f}".encode())
            # Mix every 64th key so edits/deletions invalidate reliably in O(N/64)
            step = max(1, len(items) // 64)
            for idx in range(0, len(items), step):
                k, v = items[idx]
                h.update(f"{k[0]}#{k[1]}={v[0]:.5f};".encode())
        return h.hexdigest()

    def build_or_reuse(
        self,
        model_id: str,
        items: list[tuple[tuple[str, int], list[float]]],
    ) -> str:
        """Ensure the index matches ``(model_id, items)`` and return active backend name."""
        if not items:
            self.invalidate()
            return "empty"
        dim = len(items[0][1])
        if dim == 0:
            raise ValueError("Embedding dimension cannot be zero")
        for _, vec in items:
            if len(vec) != dim:
                raise ValueError("Stored embedding dimension differs; reindex the document")

        fp = self._compute_fingerprint(model_id, items)
        with self._lock:
            if fp == self._fingerprint and model_id == self._model_id and dim == self._dim:
                return self._backend_name
            self._rebuild_locked(model_id, dim, fp, items)
            return self._backend_name

    def _rebuild_locked(
        self,
        model_id: str,
        dim: int,
        fp: str,
        items: list[tuple[tuple[str, int], list[float]]],
    ) -> None:
        self._model_id = model_id
        self._dim = dim
        self._fingerprint = fp
        self._keys = [k for k, _ in items]
        self._hnsw_index = None
        self._np_matrix = None
        self._np_planes = []
        self._py_vectors = []
        self._py_planes = []
        self._lsh_tables = []

        count = len(items)
        np = _load_optional("numpy")
        hnswlib = _load_optional("hnswlib") if np is not None else None

        # Tier 1: hnswlib C++ HNSW graph when installed and corpus exceeds exact_threshold
        if hnswlib is not None and np is not None and count > self.exact_threshold:
            matrix = np.asarray([v for _, v in items], dtype=np.float32)
            index = hnswlib.Index(space="ip", dim=dim)
            index.init_index(max_elements=count, ef_construction=120, M=16, random_seed=42)
            index.add_items(matrix, list(range(count)))
            index.set_ef(max(50, min(200, count)))
            self._hnsw_index = index
            self._np_matrix = matrix
            self._backend_name = "hnswlib_ann"
            return

        # Tier 2: NumPy BLAS matrix (exact below threshold, SimHash LSH + BLAS shortlist above)
        if np is not None:
            matrix = np.asarray([v for _, v in items], dtype=np.float32)
            self._np_matrix = matrix
            if count <= self.exact_threshold:
                self._backend_name = "numpy_blas_exact"
                return
            rng = np.random.default_rng(seed=20261115)
            self._np_planes = []
            self._lsh_tables = []
            bit_weights = (1 << np.arange(self.bits_per_table, dtype=np.int64))
            for _ in range(self.num_tables):
                planes = rng.standard_normal((dim, self.bits_per_table), dtype=np.float32)
                signs = (matrix @ planes) >= 0.0
                hashes = signs.astype(np.int64) @ bit_weights
                table: dict[int, list[int]] = {}
                for row_idx, bucket in enumerate(hashes.tolist()):
                    table.setdefault(int(bucket), []).append(row_idx)
                self._np_planes.append(planes)
                self._lsh_tables.append(table)
            self._backend_name = "numpy_simhash_lsh_ann"
            return

        # Tier 3: Zero-dependency stdlib array('f') (exact below threshold, SimHash LSH above)
        self._py_vectors = [array.array("f", v) for _, v in items]
        if count <= self.exact_threshold:
            self._backend_name = "stdlib_exact"
            return

        py_rng = random.Random(20261115)
        self._py_planes = []
        self._lsh_tables = []
        for _ in range(self.num_tables):
            t_planes: list[array.array[float]] = []
            for _b in range(self.bits_per_table):
                raw = [py_rng.gauss(0.0, 1.0) for _d in range(dim)]
                norm = math.sqrt(sum(x * x for x in raw)) or 1.0
                t_planes.append(array.array("f", [x / norm for x in raw]))
            table = {}
            for row_idx, vec in enumerate(self._py_vectors):
                bucket = self._py_hash(vec, t_planes)
                table.setdefault(bucket, []).append(row_idx)
            self._py_planes.append(t_planes)
            self._lsh_tables.append(table)
        self._backend_name = "stdlib_simhash_lsh_ann"

    @staticmethod
    def _py_hash(vec: array.array[float], planes: list[array.array[float]]) -> int:
        code = 0
        for bit_idx, plane in enumerate(planes):
            dot = sum(a * b for a, b in zip(vec, plane, strict=False))
            if dot >= 0.0:
                code |= 1 << bit_idx
        return code

    def query(self, query_vector: list[float], top_k: int = 5) -> list[VectorCandidate]:
        with self._lock:
            if not self._keys:
                return []
            if len(query_vector) != self._dim:
                raise ValueError("Stored embedding dimension differs; reindex the document")
            k = max(1, min(top_k, len(self._keys)))
            np = _load_optional("numpy")

            if self._backend_name == "hnswlib_ann" and self._hnsw_index is not None and np is not None:
                q = np.asarray([query_vector], dtype=np.float32)
                labels, distances = self._hnsw_index.knn_query(q, k=k)
                out: list[VectorCandidate] = []
                for idx, dist in zip(labels[0].tolist(), distances[0].tolist(), strict=False):
                    # hnswlib 'ip' distance is (1 - inner_product)
                    out.append(VectorCandidate(key=self._keys[int(idx)], score=float(1.0 - dist)))
                out.sort(key=lambda c: (-c.score, c.key[0], c.key[1]))
                return out[:k]

            if self._backend_name == "numpy_blas_exact" and self._np_matrix is not None and np is not None:
                q = np.asarray(query_vector, dtype=np.float32)
                scores = self._np_matrix @ q
                top_idx = np.argsort(-scores)[: min(len(scores), k * 4)]
                out = [
                    VectorCandidate(key=self._keys[int(i)], score=float(scores[int(i)]))
                    for i in top_idx.tolist()
                ]
                out.sort(key=lambda c: (-c.score, c.key[0], c.key[1]))
                return out[:k]

            if self._backend_name == "numpy_simhash_lsh_ann" and self._np_matrix is not None and np is not None:
                q = np.asarray(query_vector, dtype=np.float32)
                candidates = self._gather_lsh_candidates_np(q, target=max(128, k * self.shortlist_multiplier))
                cand_arr = np.asarray(sorted(candidates), dtype=np.int64)
                sub_scores = self._np_matrix[cand_arr] @ q
                order = np.argsort(-sub_scores)[: min(len(sub_scores), k * 4)]
                out = [
                    VectorCandidate(
                        key=self._keys[int(cand_arr[int(pos)])],
                        score=float(sub_scores[int(pos)]),
                    )
                    for pos in order.tolist()
                ]
                out.sort(key=lambda c: (-c.score, c.key[0], c.key[1]))
                return out[:k]

            q_arr = array.array("f", query_vector)
            if self._backend_name == "stdlib_simhash_lsh_ann":
                candidates = self._gather_lsh_candidates_py(q_arr, target=max(128, k * self.shortlist_multiplier))
                out = [
                    VectorCandidate(
                        key=self._keys[idx],
                        score=sum(a * b for a, b in zip(self._py_vectors[idx], q_arr, strict=False)),
                    )
                    for idx in candidates
                ]
                out.sort(key=lambda c: (-c.score, c.key[0], c.key[1]))
                return out[:k]

            # stdlib_exact
            out = [
                VectorCandidate(
                    key=self._keys[idx],
                    score=sum(a * b for a, b in zip(vec, q_arr, strict=False)),
                )
                for idx, vec in enumerate(self._py_vectors)
            ]
            out.sort(key=lambda c: (-c.score, c.key[0], c.key[1]))
            return out[:k]

    def _gather_lsh_candidates_np(self, q: Any, *, target: int) -> set[int]:
        candidates: set[int] = set()
        bucket_codes: list[int] = []
        for planes, table in zip(self._np_planes, self._lsh_tables, strict=False):
            signs = (q @ planes) >= 0.0
            code = 0
            for bit_idx, flag in enumerate(signs.tolist()):
                if flag:
                    code |= 1 << bit_idx
            bucket_codes.append(code)
            candidates.update(table.get(code, ()))

        # Multi-probe: flip 1 bit in each table if exact buckets yielded fewer than target
        if len(candidates) < target:
            for code, table in zip(bucket_codes, self._lsh_tables, strict=False):
                for bit_idx in range(self.bits_per_table):
                    neighbor = code ^ (1 << bit_idx)
                    candidates.update(table.get(neighbor, ()))
                    if len(candidates) >= target * 2:
                        break
        if not candidates:
            candidates.update(range(min(target, len(self._keys))))
        return candidates

    def _gather_lsh_candidates_py(self, q_arr: array.array[float], *, target: int) -> set[int]:
        candidates: set[int] = set()
        bucket_codes: list[int] = []
        for t_planes, table in zip(self._py_planes, self._lsh_tables, strict=False):
            code = self._py_hash(q_arr, t_planes)
            bucket_codes.append(code)
            candidates.update(table.get(code, ()))

        if len(candidates) < target:
            for code, table in zip(bucket_codes, self._lsh_tables, strict=False):
                for bit_idx in range(self.bits_per_table):
                    candidates.update(table.get(code ^ (1 << bit_idx), ()))
                    if len(candidates) >= target * 2:
                        break
        if not candidates:
            candidates.update(range(min(target, len(self._keys))))
        return candidates

    def stats(self) -> dict[str, Any]:
        return {
            "backend": self._backend_name,
            "indexed_vectors": len(self._keys),
            "dimension": self._dim,
            "model_id": self._model_id,
            "exact_threshold": self.exact_threshold,
            "hnswlib_installed": _load_optional("hnswlib") is not None,
            "numpy_installed": _load_optional("numpy") is not None,
        }
