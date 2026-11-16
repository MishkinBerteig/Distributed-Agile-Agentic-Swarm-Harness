"""Real embeddings for search against real PostgreSQL + pgvector (docs/DESIGN-0.4.1.md): rows of
several models in one table, the embedder's loading and indexing loop with fake models, the HNSW
indexes, and the coordinator's /embeddings endpoints."""
from __future__ import annotations

import math

import pytest

from app.config import Settings
from app.embedder_api import Embedder
from app.embeddings import DEFAULT_MODEL, WORD_HASHES, Embedding, LocalEmbedder, index_name
from app.models import EmbeddingSetting, SwarmConfig, TaskCreate
from app.repo import (AgentRepo, EmbeddingRepo, LearningRepo, MemoryRepo, SwarmRepo, TaskRepo,
                      TeamRepo)

TOPICS = ("garden", "cloud", "food", "tests")


class FakeModel:
    """A tiny "meaning": one dimension per topic word, padded to `dim`; normalized. Texts that share
    a topic word are near each other, whatever else they say."""

    def __init__(self, name: str, dim: int) -> None:
        assert dim >= len(TOPICS)
        self.name, self.dim, self.calls = name, dim, 0

    def vector(self, text: str) -> list[float]:
        v = [float(text.lower().count(t)) for t in TOPICS] + [0.01] * (self.dim - len(TOPICS))
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    def encode(self, texts, kind):
        self.calls += 1
        return [self.vector(t) for t in texts]


def fake_loader(models: dict):
    def load(name, *, trust_remote_code, settings):
        if name not in models:
            raise OSError(f"404 Client Error: {name} is not on the Hub")
        return models[name]
    return load


def emb(model: FakeModel, text: str) -> Embedding:
    return Embedding(text, model.name, model.vector(text))


@pytest.fixture
async def swarm(pool):
    return await SwarmRepo(pool).create("S", "The vision", None, SwarmConfig())


@pytest.fixture
async def team(pool, swarm):
    return await TeamRepo(pool).create(swarm.id, "Team A", "Mission A")


@pytest.fixture
async def agent(pool, swarm, team):
    return await AgentRepo(pool).create(swarm.id, team.id, "mission_delivery", "Delivery-1", "fast")


SMALL = FakeModel("test/small", 6)
LARGE = FakeModel("test/large", 12)


# -- search -------------------------------------------------------------------------------------------

async def test_search_compares_a_query_only_with_rows_of_its_own_model(pool, swarm, agent):
    repo = MemoryRepo(pool)
    await repo.add(agent.id, swarm.id, "Our garden plan", embedding=emb(SMALL, "Our garden plan"))
    await repo.add(agent.id, swarm.id, "The cloud bill", embedding=emb(SMALL, "The cloud bill"))
    await repo.add(agent.id, swarm.id, "Food for the party", embedding=emb(LARGE, "Food for the party"))
    await repo.add(agent.id, swarm.id, "Words only", embedding=await LocalEmbedder().document("Words only"))
    await repo.add(agent.id, swarm.id, "No vector yet")             # embedder away: text only

    # three vector sizes in one column, and no "different vector dimensions" error
    hits = await repo.search(agent.id, emb(SMALL, "cloud costs"), k=2)
    assert [m.content for m in hits] == ["The cloud bill", "Our garden plan"]
    hits = await repo.search(agent.id, emb(LARGE, "food"), k=1)
    assert [m.content for m in hits] == ["Food for the party"]
    # fewer rows of the query's model than k: the newest others follow
    hits = await repo.search(agent.id, emb(LARGE, "food"), k=3)
    assert [m.content for m in hits] == ["Food for the party", "No vector yet", "Words only"]
    assert len(await repo.search(agent.id, None, k=8)) == 5          # recency without a query
    assert [m.content for m in await repo.search(agent.id, Embedding("cloud"), k=1)] == ["No vector yet"]


async def test_learnings_search_keeps_its_scope(pool, swarm, team):
    repo = LearningRepo(pool)
    await SwarmRepo(pool).update(swarm.id, lifecycle_state="ARCHIVED")   # one live swarm at a time
    other = await SwarmRepo(pool).create("Other", "v", None, SwarmConfig())
    await repo.add("mine: tests first", "pattern", swarm_id=swarm.id, embedding=emb(SMALL, "tests"))
    await repo.add("global: garden", "pattern", swarm_id=None, embedding=emb(SMALL, "garden"))
    await repo.add("theirs: tests", "pattern", swarm_id=other.id, embedding=emb(SMALL, "tests"))
    hits = await repo.search(emb(SMALL, "run the tests"), swarm.id, k=8)
    assert [x.content for x in hits] == ["mine: tests first", "global: garden"]
    hits = await repo.search(emb(SMALL, "garden"), swarm.id, k=8, include_global=False)
    assert [x.content for x in hits] == ["mine: tests first"]


async def test_rows_keep_their_text_and_a_renamed_task_is_embedded_again(pool, swarm, team):
    tasks = TaskRepo(pool)
    data = TaskCreate(team_id=team.id, name="Plan the garden", description="Beds and paths")
    t = await tasks.create(swarm.id, data, embedding=emb(SMALL, "Plan the garden\nBeds and paths"))
    bare = await tasks.create(swarm.id, TaskCreate(team_id=team.id, name="Bare task"))
    row = await pool.fetchrow("SELECT embedding_model, embedding_text, embedding IS NOT NULL AS has "
                              "FROM tasks WHERE id = $1", t.id)
    assert (row["embedding_model"], row["embedding_text"], row["has"]) == (
        "test/small", "Plan the garden\nBeds and paths", True)
    assert await pool.fetchval("SELECT embedding_text FROM tasks WHERE id = $1", bare.id) == "Bare task"

    await tasks.update(t.id, priority=2)                              # not a rename: kept
    assert await pool.fetchval("SELECT embedding_model FROM tasks WHERE id = $1", t.id) == "test/small"
    await tasks.update(t.id, name="Plan the cloud")
    row = await pool.fetchrow("SELECT embedding_model, embedding_text, embedding FROM tasks "
                              "WHERE id = $1", t.id)
    assert (row["embedding_model"], row["embedding"]) == (None, None)
    assert row["embedding_text"] == "Plan the cloud\nBeds and paths"


# -- the setting and the status -------------------------------------------------------------------------

async def test_embedding_setting_and_status(pool):
    repo = EmbeddingRepo(pool)
    assert await repo.get_setting(DEFAULT_MODEL) == EmbeddingSetting(model=DEFAULT_MODEL)
    await repo.put_setting(EmbeddingSetting(model="test/small", trust_remote_code=True))
    assert await repo.get_setting(DEFAULT_MODEL) == EmbeddingSetting(model="test/small",
                                                                     trust_remote_code=True)
    await pool.execute("UPDATE global_config SET value = '{\"model\": \"bad name\"}' "
                       "WHERE key = 'embedding'")
    assert (await repo.get_setting(DEFAULT_MODEL)).model == DEFAULT_MODEL   # never a bad name
    assert (await repo.status())["state"] == "starting"                    # no row yet
    await repo.set_status(state="indexing", active_model="test/small", dim=6, indexed=1, total=3)
    st = await repo.status()
    assert (st["state"], st["active_model"], st["dim"], st["indexed"], st["total"]) == (
        "indexing", "test/small", 6, 1, 3)
    with pytest.raises(ValueError):
        await repo.set_status(updated_at=None)


# -- the embedder's loop ---------------------------------------------------------------------------------

async def seed(pool, swarm, team, agent) -> None:
    await TaskRepo(pool).create(swarm.id, TaskCreate(team_id=team.id, name="Garden beds"))
    await LearningRepo(pool).add("Cloud bills grow quietly", "pattern", swarm_id=swarm.id)
    mem = MemoryRepo(pool)
    await mem.add(agent.id, swarm.id, "Bob likes food with cheese")
    await mem.add(agent.id, swarm.id, "The tests live in tests/unit")
    await mem.add(agent.id, swarm.id, "Our garden has three beds")


async def run_until_idle(e: Embedder, limit: int = 20) -> None:
    for _ in range(limit):
        if not await e.step():
            return
    raise AssertionError("the embedder never finished indexing")


async def test_the_embedder_indexes_everything_then_swaps_models_and_indexes(pool, swarm, team, agent):
    await seed(pool, swarm, team, agent)
    repo = EmbeddingRepo(pool)
    small, large = FakeModel("test/small", 6), FakeModel("test/large", 12)
    settings = Settings(EMBEDDING_MODEL="test/small")
    e = Embedder(repo, settings, loader=fake_loader({"test/small": small, "test/large": large}))

    await run_until_idle(e)
    st = await repo.status()
    assert (st["state"], st["active_model"], st["dim"], st["indexed"], st["total"]) == (
        "ready", "test/small", 6, 5, 5)
    assert await repo.counts("test/small") == {"tasks": {"total": 1, "indexed": 1},
                                               "learnings": {"total": 1, "indexed": 1},
                                               "memories": {"total": 3, "indexed": 3}}
    assert await repo.vector_indexes() == {t: [index_name(t, "test/small", 6)]
                                           for t in ("tasks", "learnings", "memories")}
    hits = await MemoryRepo(pool).search(agent.id, emb(small, "garden"), k=1)
    assert hits[0].content == "Our garden has three beds"

    # the user chooses another model with another size
    await repo.put_setting(EmbeddingSetting(model="test/large"))
    await run_until_idle(e)
    st = await repo.status()
    assert (st["state"], st["active_model"], st["dim"], st["indexed"], st["total"], st["error"]) == (
        "ready", "test/large", 12, 5, 5, None)
    assert await repo.vector_indexes() == {t: [index_name(t, "test/large", 12)]
                                           for t in ("tasks", "learnings", "memories")}
    assert await pool.fetchval("SELECT count(*) FROM memories WHERE embedding_model = 'test/large' "
                               "AND vector_dims(embedding) = 12") == 3
    hits = await MemoryRepo(pool).search(agent.id, emb(large, "who likes food"), k=1)
    assert hits[0].content == "Bob likes food with cheese"

    # a model that cannot be loaded: the one in use stays, and the error is shown
    await repo.put_setting(EmbeddingSetting(model="someone/missing"))
    await run_until_idle(e)
    st = await repo.status()
    assert (st["active_model"], st["chosen_model"], st["state"]) == ("test/large", "someone/missing",
                                                                     "ready")
    assert "Could not load someone/missing" in st["error"]
    # new rows written meanwhile are embedded by the model in use
    await MemoryRepo(pool).add(agent.id, swarm.id, "Cloud notes")
    await run_until_idle(e)
    assert (await repo.status())["indexed"] == 6


async def test_the_active_models_hnsw_index_serves_filtered_searches(pool, swarm, team, agent):
    await seed(pool, swarm, team, agent)
    repo = EmbeddingRepo(pool)
    small = FakeModel("test/small", 6)
    e = Embedder(repo, Settings(EMBEDDING_MODEL="test/small"), loader=fake_loader({"test/small": small}))
    await run_until_idle(e)
    name = index_name("memories", "test/small", 6)
    q = "[" + ",".join(str(x) for x in small.vector("garden")) + "]"
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off")
        await conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
        plan = "\n".join(r[0] for r in await conn.fetch(
            "EXPLAIN SELECT id FROM memories WHERE agent_id = $1 AND embedding_model = 'test/small' "
            "AND embedding IS NOT NULL ORDER BY embedding::vector(6) <=> $2::vector(6) LIMIT 3",
            agent.id, q))
    assert f"Index Scan using {name}" in plan, plan


async def test_word_hashes_are_a_model_like_any_other(pool, swarm, team, agent):
    await seed(pool, swarm, team, agent)
    repo = EmbeddingRepo(pool)
    from app.embed_models import load_model
    e = Embedder(repo, Settings(EMBEDDING_MODEL=WORD_HASHES), loader=load_model)
    await run_until_idle(e)
    st = await repo.status()
    assert (st["state"], st["active_model"], st["dim"], st["indexed"]) == ("ready", WORD_HASHES, 768, 5)
    q = await LocalEmbedder().query("garden has three beds")
    assert (await MemoryRepo(pool).search(agent.id, q, k=1))[0].content == "Our garden has three beds"


# -- the coordinator's API -------------------------------------------------------------------------------

async def test_embeddings_endpoints(api, pool):
    r = await api.get("/embeddings")
    assert r.status_code == 200
    body = r.json()
    assert body["setting"] == {"model": DEFAULT_MODEL, "trust_remote_code": False}
    assert body["default_model"] == DEFAULT_MODEL
    assert body["catalog"][0]["name"] == DEFAULT_MODEL and body["catalog"][0]["built_in"]
    assert body["service"] == {"configured": False, "reachable": False, "health": None}
    assert set(body["counts"]) == {"tasks", "learnings", "memories"}
    assert set(body["indexes"]) == {"tasks", "learnings", "memories"}

    r = await api.put("/embeddings", json={"model": "BAAI/bge-small-en-v1.5"})
    assert r.status_code == 200
    assert r.json()["setting"] == {"model": "BAAI/bge-small-en-v1.5", "trust_remote_code": False}
    assert (await EmbeddingRepo(pool).get_setting(DEFAULT_MODEL)).model == "BAAI/bge-small-en-v1.5"
    r = await api.put("/embeddings", json={"model": "x'; DROP TABLE memories; --"})
    assert r.status_code == 422
    assert (await EmbeddingRepo(pool).get_setting(DEFAULT_MODEL)).model == "BAAI/bge-small-en-v1.5"
