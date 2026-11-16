"""Embeddings for search (docs/DESIGN-0.4.1.md): clients, names, the catalog, the service API."""
from __future__ import annotations

import json
import math

import httpx
import pytest
from pydantic import ValidationError

from app import embed_models, embedder_api
from app.config import Settings
from app.embeddings import (CATALOG, DEFAULT_MODEL, WORD_HASH_DIM, WORD_HASHES, Embedding,
                            EmbedderClient, LocalEmbedder, build_embedder, catalog_entry,
                            index_name, sql_literal, task_text, trusts_remote_code,
                            valid_model_name, word_hash_vector)
from app.models import EmbeddingSetting


def test_default_embedding_model_is_gte_base():
    assert Settings().EMBEDDING_MODEL == DEFAULT_MODEL == "Alibaba-NLP/gte-base-en-v1.5"
    assert Settings().EMBEDDER_URL == ""          # compose sets http://embedder:8002


def test_word_hashes_are_deterministic_normalized_and_vary():
    v1 = word_hash_vector("ship the daashboard with swarm inspection")
    assert v1 == word_hash_vector("ship the daashboard with swarm inspection")
    assert len(v1) == WORD_HASH_DIM == 768
    assert math.isclose(math.sqrt(sum(x * x for x in v1)), 1.0, rel_tol=1e-9)
    assert word_hash_vector("alpha beta") != word_hash_vector("gamma delta")
    assert word_hash_vector("") == [0.0] * WORD_HASH_DIM


@pytest.mark.parametrize("name", [DEFAULT_MODEL, "BAAI/bge-small-en-v1.5", WORD_HASHES,
                                  "sentence-transformers/all-MiniLM-L6-v2", "e5_base.v2"])
def test_valid_model_names(name):
    assert valid_model_name(name)
    assert sql_literal(name) == f"'{name}'"


@pytest.mark.parametrize("name", ["", " ", "a/b/c", "/abc", "abc/", "bad name", "x'; DROP TABLE t;--",
                                  "-flag", "é/model", "a" * 121, None, 7])
def test_invalid_model_names_never_reach_sql(name):
    assert not valid_model_name(name)
    with pytest.raises(ValueError):
        sql_literal(name)


def test_the_catalog_starts_with_the_built_in_model_and_decides_remote_code():
    assert CATALOG[0].name == DEFAULT_MODEL and CATALOG[0].built_in
    assert [m.name for m in CATALOG if m.built_in] == [DEFAULT_MODEL]
    assert len({m.name for m in CATALOG}) == len(CATALOG)
    assert all(valid_model_name(m.name) and m.dim > 0 and m.note for m in CATALOG)
    assert catalog_entry(WORD_HASHES).size_mb == 0
    # the catalog decides for its own models; the user decides for any other
    assert trusts_remote_code(DEFAULT_MODEL, False) is True
    assert trusts_remote_code("BAAI/bge-small-en-v1.5", True) is False
    assert trusts_remote_code("someone/custom-model", True) is True
    assert trusts_remote_code("someone/custom-model", False) is False


def test_task_text_and_index_names():
    assert task_text("Write README", "One example per function.") == \
        "Write README\nOne example per function."
    assert task_text("Write README", None) == task_text("Write README", "  ") == "Write README"
    a = index_name("memories", DEFAULT_MODEL, 768)
    assert a == index_name("memories", DEFAULT_MODEL, 768) and a.startswith("memories_emb_")
    assert a != index_name("memories", "BAAI/bge-small-en-v1.5", 384)
    assert a != index_name("learnings", DEFAULT_MODEL, 768)
    assert len(a) <= 63                           # PostgreSQL identifier limit


def test_embedding_setting_validates_the_model_name():
    assert EmbeddingSetting().model == DEFAULT_MODEL
    assert EmbeddingSetting(model="  BAAI/bge-small-en-v1.5 ").model == "BAAI/bge-small-en-v1.5"
    with pytest.raises(ValidationError):
        EmbeddingSetting(model="not a model")


async def test_local_embedder_tags_its_vectors_with_word_hashes():
    e = LocalEmbedder()
    doc, q = await e.document("compost heap"), await e.query("compost heap")
    assert doc.ok and doc.model == WORD_HASHES and doc.dim == WORD_HASH_DIM and doc.text == "compost heap"
    assert doc.vector == q.vector == word_hash_vector("compost heap")
    empty = await e.document("   ")
    assert not empty.ok and empty.vector is None and empty.text == ""


def _client(handler) -> EmbedderClient:
    return EmbedderClient("http://embedder:8002", transport=httpx.MockTransport(handler))


async def test_embedder_client_posts_the_kind_and_returns_the_model():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"model": "BAAI/bge-small-en-v1.5", "dim": 3,
                                         "vectors": [[0.1, 0.2, 0.3]]})

    c = _client(handler)
    q = await c.query("what food does Bob enjoy")
    d = await c.document("Bob likes pizza")
    assert q == Embedding("what food does Bob enjoy", "BAAI/bge-small-en-v1.5", [0.1, 0.2, 0.3])
    assert d.ok and d.dim == 3
    assert seen == [("/embed", {"texts": ["what food does Bob enjoy"], "kind": "query"}),
                    ("/embed", {"texts": ["Bob likes pizza"], "kind": "document"})]


@pytest.mark.parametrize("response", [
    httpx.Response(503, json={"detail": "no embedding model is loaded yet"}),
    httpx.Response(200, content=b"not json"),
    httpx.Response(200, json={"model": "bad name", "vectors": [[1.0]]}),
    httpx.Response(200, json={"model": "a/b", "vectors": []}),
    httpx.Response(200, json={"model": "a/b", "vectors": [[]]}),
    httpx.Response(200, json={"vectors": [[1.0]]}),
])
async def test_embedder_client_turns_every_failure_into_no_vector(response):
    got = await _client(lambda request: response).document("text")
    assert got == Embedding("text") and not got.ok


async def test_embedder_client_survives_an_unreachable_embedder():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    got = await _client(handler).query("anything")
    assert got == Embedding("anything")


def test_build_embedder_uses_the_service_only_when_configured():
    assert isinstance(build_embedder(Settings(EMBEDDER_URL="")), LocalEmbedder)
    client = build_embedder(Settings(EMBEDDER_URL="http://embedder:8002/"))
    assert isinstance(client, EmbedderClient) and client.url == "http://embedder:8002"


# -- the embedder service (app/embed_models.py, app/embedder_api.py) ---------------------------------

def test_word_hashes_load_without_pytorch_and_other_models_need_it():
    m = embed_models.load_model(WORD_HASHES, trust_remote_code=False, settings=Settings())
    assert (m.name, m.dim) == (WORD_HASHES, WORD_HASH_DIM)
    assert m.encode(["a b"], "query") == [word_hash_vector("a b")]
    with pytest.raises(ImportError):              # the test image has no sentence-transformers
        embed_models.load_model("BAAI/bge-small-en-v1.5", trust_remote_code=False, settings=Settings())


def test_built_in_models_are_plain_folders(tmp_path):
    assert embed_models.baked_path(DEFAULT_MODEL, str(tmp_path)) is None
    folder = embed_models.baked_folder(DEFAULT_MODEL, str(tmp_path))
    assert folder.name == "Alibaba-NLP__gte-base-en-v1.5"
    folder.mkdir()
    (folder / "modules.json").write_text("[]")
    assert embed_models.baked_path(DEFAULT_MODEL, str(tmp_path)) == folder


class FakeModel:
    def __init__(self, name: str, dim: int) -> None:
        self.name, self.dim, self.calls = name, dim, []

    def encode(self, texts, kind):
        self.calls.append((list(texts), kind))
        return [[float(len(t))] + [0.0] * (self.dim - 1) for t in texts]


class FakeRepo:
    """Just enough of EmbeddingRepo for the loading logic."""

    def __init__(self, setting: EmbeddingSetting) -> None:
        self.setting, self.status = setting, {}

    async def get_setting(self, default_model):
        return self.setting

    async def set_status(self, **fields):
        self.status = fields


async def test_a_model_that_fails_to_load_at_start_falls_back_to_the_built_in_one():
    loaded = []

    def loader(name, *, trust_remote_code, settings):
        loaded.append(name)
        if name == "someone/broken":
            raise OSError("404 Client Error: Repository Not Found")
        return FakeModel(name, 8)

    repo = FakeRepo(EmbeddingSetting(model="someone/broken"))
    e = embedder_api.Embedder(repo, Settings(), loader=loader)
    await e.ensure_model()
    assert loaded == ["someone/broken", DEFAULT_MODEL]
    assert e.model.name == DEFAULT_MODEL
    assert e.error == ("Hugging Face has no public model called someone/broken. Check the name "
                       "(a private model needs HF_TOKEN in .env).")
    assert repo.status["error"] == e.error and repo.status["active_model"] == DEFAULT_MODEL
    await e.ensure_model()                         # not retried until the user chooses again
    assert loaded == ["someone/broken", DEFAULT_MODEL]
    repo.setting = EmbeddingSetting(model=DEFAULT_MODEL)
    await e.ensure_model()                         # choosing the model in use clears the error
    assert e.error is None and loaded == ["someone/broken", DEFAULT_MODEL]


async def test_embed_endpoint(monkeypatch):
    model = FakeModel("BAAI/bge-small-en-v1.5", 4)
    e = embedder_api.Embedder(FakeRepo(EmbeddingSetting()), Settings())
    monkeypatch.setattr(embedder_api, "embedder", e)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=embedder_api.app),
                                 base_url="http://embedder") as c:
        r = await c.post("/embed", json={"texts": ["abc"], "kind": "query"})
        assert r.status_code == 503                # no model loaded yet
        e.model = model
        r = await c.post("/embed", json={"texts": ["abc", "de"], "kind": "query"})
        assert r.status_code == 200
        assert r.json() == {"model": "BAAI/bge-small-en-v1.5", "dim": 4,
                            "vectors": [[3.0, 0.0, 0.0, 0.0], [2.0, 0.0, 0.0, 0.0]]}
        assert model.calls == [(["abc", "de"], "query")]
        assert (await c.post("/embed", json={"texts": ["x"], "kind": "other"})).status_code == 422
        assert (await c.post("/embed", json={"texts": []})).status_code == 422
        health = (await c.get("/health")).json()
        assert health["model"] == "BAAI/bge-small-en-v1.5" and health["dim"] == 4


def _err(kind: str, text: str = "") -> Exception:
    return type(kind, (Exception,), {})(text)


@pytest.mark.parametrize("exc, words", [
    (_err("GatedRepoError", "Cannot access gated repo"), "is gated on Hugging Face"),
    (_err("RepositoryNotFoundError", "401 Client Error"), "has no public model called x/y"),
    (ValueError("requires you to execute the configuration file in that repo on your local machine. "
                "Make sure you have read the code there to avoid malicious use, then set the option "
                "`trust_remote_code=True` to remove this error."), "ships its own code"),
    (_err("LocalEntryNotFoundError", "cannot find the requested files"), "could not reach Hugging Face"),
    (ImportError("No module named 'sentence_transformers'"), "can only use word hashes"),
    (RuntimeError("x" * 400), "Could not load x/y: RuntimeError: xxx"),
])
def test_load_errors_are_explained_in_plain_words(exc, words):
    text = embed_models.explain_load_error("x/y", exc)
    assert words in text and len(text) <= 340
