"""Slice 1.5: embedding populated at task creation (gte-base-en-v1.5 default)."""
from __future__ import annotations

import math

import pytest

from app.config import Settings
from app.embeddings import EMBEDDING_DIM, HashingEmbedder, LazyEmbedder, build_embedder
from app.models import TaskCreate


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


@pytest.mark.asyncio
async def test_repo_persists_embedding(task_repo, pool):
    team_id = "team-emb"
    await pool.execute(
        "INSERT INTO teams (id, name, vision_statement, mission_statement) VALUES ($1, $1, 'v', '')",
        team_id,
    )
    task = await task_repo.create(
        TaskCreate(team_id="team-emb", name="Vectorize me", description="d", acceptance_criteria="c"),
        embedding=HashingEmbedder().embed("vectorize me d c"),
    )
    row = await pool.fetchrow(
        "SELECT embedding IS NOT NULL AS has_vec, vector_dims(embedding) AS dim FROM tasks WHERE id = $1",
        task.id,
    )
    assert row["has_vec"] is True
    assert row["dim"] == 768


@pytest.mark.asyncio
async def test_repo_embedding_null_when_omitted(task_repo, pool):
    team_id = "team-emb-null"
    await pool.execute(
        "INSERT INTO teams (id, name, vision_statement, mission_statement) VALUES ($1, $1, 'v', '')",
        team_id,
    )
    task = await task_repo.create(
        TaskCreate(team_id=team_id, name="No vector", description="", acceptance_criteria="")
    )
    row = await pool.fetchrow("SELECT embedding FROM tasks WHERE id = $1", task.id)
    assert row["embedding"] is None


@pytest.mark.asyncio
async def test_create_task_api_populates_embedding(client, pool):
    resp = await client.post(
        "/tasks",
        json={
            "team_id": "team-api-emb",
            "name": "Embed me via API",
            "description": "slice one point five",
            "acceptance_criteria": "embedding column is populated",
        },
    )
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]

    row = await pool.fetchrow(
        "SELECT embedding IS NOT NULL AS has_vec, vector_dims(embedding) AS dim FROM tasks WHERE id = $1",
        task_id,
    )
    assert row["has_vec"] is True
    assert row["dim"] == 768

    # Matches the embedder's output for the composed text (deterministic fallback).
    expected = HashingEmbedder().embed(
        "Embed me via API\nslice one point five\nembedding column is populated"
    )
    stored = await pool.fetchval("SELECT embedding::text FROM tasks WHERE id = $1", task_id)
    got = [float(x) for x in stored.strip()[1:-1].split(",")]
    assert len(got) == 768
    assert all(abs(a - b) < 1e-5 for a, b in zip(got, expected))


@pytest.mark.asyncio
async def test_task_reads_exclude_embedding_column(client):
    resp = await client.post("/tasks", json={"team_id": "team-api-read", "name": "Roundtrip"})
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]

    got = await client.get(f"/tasks/{task_id}")
    assert got.status_code == 200
    assert "embedding" not in got.json()

    listing = await client.get("/tasks", params={"team_id": "team-api-read"})
    assert listing.status_code == 200
    assert all("embedding" not in t for t in listing.json()["tasks"])
