"""Slice 1.5: embedding populated at task creation (gte-base-en-v1.5 default)."""
from __future__ import annotations

import math

import pytest

from app.config import Settings
from app.embeddings import EMBEDDING_DIM, HashingEmbedder, LazyEmbedder, build_embedder


def test_default_embedding_model_is_gte_base():
    assert Settings().EMBEDDING_MODEL == "Alibaba-NLP/gte-base-en-v1.5"


def test_hashing_embedder_deterministic_and_normalized():
    e = HashingEmbedder()
    v1 = e.embed("ship the daashboard with swarm inspection")
    v2 = e.embed("ship the daashboard with swarm inspection")
    assert v1 == v2
    assert len(v1) == EMBEDDING_DIM == 768
    assert math.isclose(math.sqrt(sum(x * x for x in v1)), 1.0, rel_tol=1e-9)


def test_hashing_embedder_varies_with_text():
    e = HashingEmbedder()
    assert e.embed("alpha beta") != e.embed("gamma delta")


def test_lazy_embedder_falls_back_when_disabled():
    emb = build_embedder("Alibaba-NLP/gte-base-en-v1.5", enabled=False)
    assert isinstance(emb, LazyEmbedder)
    vec = emb.embed("hello world")
    assert len(vec) == 768
    assert emb._resolved.name == "hashing-fallback"


def test_lazy_embedder_falls_back_when_model_unavailable(monkeypatch):
    import app.embeddings as embeddings

    def boom(model_name):
        raise RuntimeError("no model, no network")

    monkeypatch.setattr(embeddings, "SentenceTransformersEmbedder", boom)
    emb = build_embedder("Alibaba-NLP/gte-base-en-v1.5", enabled=True)
    vec = emb.embed("hello world")
    assert len(vec) == 768
    assert emb._resolved.name == "hashing-fallback"
