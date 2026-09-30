"""Embedding providers for DAASH memory vectors.

The primary model is Alibaba-NLP/gte-base-en-v1.5 (768-dim), loaded lazily via
sentence-transformers when installed and enabled. A deterministic hash-based
fallback keeps the harness fully operational (and testable) without a model
download; both produce 768-dim vectors matching the pgvector columns.
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
from typing import Protocol

log = logging.getLogger(__name__)

EMBEDDING_DIM = 768


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Deterministic fallback embedder: feature-hashed token counts, L2-normalized.

    Not semantically meaningful, but stable across processes and free of any
    model download — good enough to exercise the storage/retrieval plumbing.
    """

    name = "hashing-fallback"
    dim = EMBEDDING_DIM

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class SentenceTransformersEmbedder:
    """Thin wrapper around a sentence-transformers model (default gte-base-en-v1.5)."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # lazy: heavy dep

        self.name = model_name
        log.info("Loading embedding model %s ...", model_name)
        self._model = SentenceTransformer(model_name)
        self.dim = int(self._model.get_sentence_embedding_dimension())
        if self.dim != EMBEDDING_DIM:
            raise ValueError(
                f"embedding model {model_name} produces {self.dim}-dim vectors, "
                f"but the schema expects {EMBEDDING_DIM}"
            )

    def embed(self, text: str) -> list[float]:
        return [float(x) for x in self._model.encode(text, normalize_embeddings=True)]


class LazyEmbedder:
    """Resolves to the primary model on first use; falls back if unavailable."""

    dim = EMBEDDING_DIM

    def __init__(self, model_name: str, enabled: bool) -> None:
        self._model_name = model_name
        self._enabled = enabled
        self._lock = threading.Lock()
        self._resolved: Embedder | None = None
        self.name = f"lazy({model_name})"

    def _resolve(self) -> Embedder:
        if self._resolved is not None:
            return self._resolved
        with self._lock:
            if self._resolved is None:
                if self._enabled:
                    try:
                        self._resolved = SentenceTransformersEmbedder(self._model_name)
                    except Exception:  # noqa: BLE001 - any import/download/load failure
                        log.exception(
                            "Embedding model %s unavailable; using deterministic fallback.",
                            self._model_name,
                        )
                        self._resolved = HashingEmbedder()
                else:
                    self._resolved = HashingEmbedder()
        return self._resolved

    def embed(self, text: str) -> list[float]:
        return self._resolve().embed(text)


def build_embedder(model_name: str, enabled: bool) -> LazyEmbedder:
    return LazyEmbedder(model_name, enabled)
