"""Embeddings for DAASH's search (docs/DESIGN-0.4.1.md).

Every task, learning and memory row keeps the text it is found by, a vector, and the name of the
model that made the vector. The model runs in one place, the `embedder` service
(app/embedder_api.py); the other processes reach it with `EmbedderClient`. `LocalEmbedder` embeds
in-process with word hashes (no model, no network): the unit and integration tests use it, and so
does any process started without DAASH_EMBEDDER_URL.

Clients never raise. When the embedder cannot be reached, the `Embedding` has no vector: the row
keeps its text and the embedder's indexer embeds it later, and a search without a query vector
falls back to recency.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import httpx

log = logging.getLogger(__name__)

#: The model built into the embedder image (Dockerfile, target `embedder`).
DEFAULT_MODEL = "Alibaba-NLP/gte-base-en-v1.5"
#: The "model" with no model: feature-hashed word counts. Matches shared words, not meaning.
WORD_HASHES = "word-hashes"
WORD_HASH_DIM = 768
EMBED_KINDS = ("query", "document")
#: The tables whose rows are embedded and searched (each has embedding, embedding_model,
#: embedding_text).
EMBEDDED_TABLES = ("tasks", "learnings", "memories")

# A Hugging Face model id ("org/name" or "name") or word-hashes. ASCII only, no quotes or spaces:
# the name goes into SQL as a literal (so the planner can use the model's partial HNSW index).
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)?$")
MAX_MODEL_NAME = 120


def valid_model_name(name: Any) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= MAX_MODEL_NAME
            and bool(_MODEL_NAME.match(name)))


def sql_literal(model: str) -> str:
    """`model` as a quoted SQL literal. Raises ValueError for anything that is not a model name."""
    if not valid_model_name(model):
        raise ValueError(f"not a model name: {model!r}")
    return "'" + model.replace("'", "''") + "'"


@dataclass(frozen=True)
class CatalogModel:
    """A model DAASHboard offers by name (any other Hugging Face model can be typed in)."""
    name: str
    label: str
    dim: int
    size_mb: int
    note: str
    trust_remote_code: bool = False
    built_in: bool = False


CATALOG: tuple[CatalogModel, ...] = (
    CatalogModel(DEFAULT_MODEL, "GTE base v1.5", 768, 550,
                 "Built in: no download. English; reads long texts.",
                 trust_remote_code=True, built_in=True),
    CatalogModel("BAAI/bge-small-en-v1.5", "BGE small v1.5", 384, 130, "Small and fast. English."),
    CatalogModel("BAAI/bge-base-en-v1.5", "BGE base v1.5", 768, 440, "English."),
    CatalogModel("sentence-transformers/all-MiniLM-L6-v2", "MiniLM L6 v2", 384, 90,
                 "The smallest and fastest. English; short texts."),
    CatalogModel("mixedbread-ai/mxbai-embed-large-v1", "mxbai embed large v1", 1024, 1340,
                 "The most accurate here, and the slowest. English."),
    CatalogModel(WORD_HASHES, "Word hashes (no model)", WORD_HASH_DIM, 0,
                 "No model: matches texts that share words, not meaning."),
)


def catalog_entry(name: str) -> Optional[CatalogModel]:
    return next((m for m in CATALOG if m.name == name), None)


def trusts_remote_code(name: str, requested: bool) -> bool:
    """Whether loading `name` may run the model's own code: the catalog decides for its models
    (DAASH vetted them); for any other model, the user's explicit choice."""
    entry = catalog_entry(name)
    return entry.trust_remote_code if entry else bool(requested)


def word_hash_vector(text: str) -> list[float]:
    """Deterministic feature-hashed token counts, L2-normalized (WORD_HASH_DIM dims)."""
    vec = [0.0] * WORD_HASH_DIM
    for token in text.lower().split():
        h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
        vec[h % WORD_HASH_DIM] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec


def index_name(table: str, model: str, dim: int) -> str:
    """The name of `table`'s HNSW index for `model`'s vectors: `<table>_emb_<hash>`."""
    key = hashlib.sha1(f"{model}|{int(dim)}".encode()).hexdigest()[:12]
    return f"{table}_emb_{key}"


def task_text(name: str, description: Optional[str] = None) -> str:
    """What a task is found by: its name and description."""
    return f"{name or ''}\n{description or ''}".strip()


@dataclass(frozen=True)
class Embedding:
    """A text and, when the embedder answered, its vector and the model that made it."""
    text: str
    model: Optional[str] = None
    vector: Optional[list[float]] = None

    @property
    def ok(self) -> bool:
        return bool(self.vector) and valid_model_name(self.model)

    @property
    def dim(self) -> int:
        return len(self.vector) if self.vector else 0


class _Embedder:
    """`document(text)` for what is stored, `query(text)` for what is searched with. Models with
    query and document prompts (e5, nomic, mxbai...) get them in the embedder service."""

    async def _embed(self, texts: Sequence[str], kind: str) -> Optional[tuple[str, list[list[float]]]]:
        raise NotImplementedError

    async def _one(self, text: str, kind: str) -> Embedding:
        text = (text or "").strip()
        if not text:
            return Embedding(text)
        got = await self._embed([text], kind)
        if not got:
            return Embedding(text)
        model, vectors = got
        return Embedding(text, model, vectors[0])

    async def document(self, text: str) -> Embedding:
        return await self._one(text, "document")

    async def query(self, text: str) -> Embedding:
        return await self._one(text, "query")


class LocalEmbedder(_Embedder):
    """Word hashes, in-process (tests; a process with no DAASH_EMBEDDER_URL)."""
    model = WORD_HASHES

    async def _embed(self, texts: Sequence[str], kind: str) -> Optional[tuple[str, list[list[float]]]]:
        return WORD_HASHES, [word_hash_vector(t) for t in texts]


class EmbedderClient(_Embedder):
    """The embedder service over HTTP (POST /embed). Any failure gives no vector, never an error."""

    #: seconds between warnings while the embedder is unreachable (each failure is logged at DEBUG)
    WARN_EVERY = 60.0

    def __init__(self, url: str, *, timeout: float = 20.0, transport: Any = None) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.transport = transport
        self._warned = 0.0

    def _warn(self, msg: str, *args: Any) -> None:
        now = time.monotonic()
        if now - self._warned >= self.WARN_EVERY:
            self._warned = now
            log.warning(msg + " (searches use recency until it answers)", *args)
        else:
            log.debug(msg, *args)

    async def _embed(self, texts: Sequence[str], kind: str) -> Optional[tuple[str, list[list[float]]]]:
        try:
            async with httpx.AsyncClient(base_url=self.url, timeout=self.timeout,
                                         transport=self.transport) as client:
                r = await client.post("/embed", json={"texts": list(texts), "kind": kind})
        except httpx.HTTPError as exc:
            self._warn("embedder at %s unreachable: %r", self.url, exc)
            return None
        if r.status_code != 200:
            self._warn("embedder at %s answered %s: %s", self.url, r.status_code, r.text[:200])
            return None
        try:
            body = r.json()
            model, vectors = body["model"], body["vectors"]
        except (ValueError, KeyError, TypeError) as exc:
            self._warn("embedder at %s sent a bad answer: %r", self.url, exc)
            return None
        if (not valid_model_name(model) or not isinstance(vectors, list) or len(vectors) != len(texts)
                or not all(isinstance(v, list) and v for v in vectors)
                or len({len(v) for v in vectors}) != 1):
            self._warn("embedder at %s sent a bad answer (model %r)", self.url, model)
            return None
        return model, [[float(x) for x in v] for v in vectors]


def build_embedder(settings: Any) -> _Embedder:
    """The embedder service when DAASH_EMBEDDER_URL is set, else word hashes in-process."""
    url = (getattr(settings, "EMBEDDER_URL", "") or "").strip()
    return EmbedderClient(url) if url else LocalEmbedder()
