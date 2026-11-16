"""0.4.1 end to end (docs/DESIGN-0.4.1.md section 7): the real embedder container with its built-in
model, on a network with no way out. The coordinator and the engine use it for every task, learning
and memory they store and for every search (DAASH_EMBEDDER_URL in docker-compose.e2e.yaml)."""
from __future__ import annotations

import os

import httpx
import pytest

from tests.e2e.helpers import poll

EMBEDDER_URL = os.environ.get("E2E_EMBEDDER_URL", "http://embedder:8002")
MODEL = "Alibaba-NLP/gte-base-en-v1.5"

DOCS = ["The deployment runs on Kubernetes in Google Cloud.",
        "Bob likes pizza with extra cheese.",
        "Unit tests live in tests/unit and run with pytest."]
QUERIES = ["cloud infrastructure", "what food does Bob enjoy", "how do I run the test suite"]


def embed(texts: list[str], kind: str) -> dict:
    r = httpx.post(EMBEDDER_URL + "/embed", json={"texts": texts, "kind": kind}, timeout=60)
    assert r.status_code == 200, r.text
    return r.json()


def embeddings_ready(api) -> dict:
    return poll(lambda: (lambda e: e if e["status"]["state"] == "ready" else None)(api.get("/embeddings")),
                timeout=180, desc="the embedder to be ready")


def test_the_built_in_model_embeds_by_meaning_with_no_network(api):
    e = embeddings_ready(api)
    assert (e["status"]["active_model"], e["status"]["dim"], e["status"]["error"]) == (MODEL, 768, None)
    assert e["service"]["configured"] and e["service"]["reachable"]

    docs, queries = embed(DOCS, "document"), embed(QUERIES, "query")
    assert (docs["model"], docs["dim"], queries["model"]) == (MODEL, 768, MODEL)
    for i, q in enumerate(queries["vectors"]):          # normalized: the dot product is the cosine
        sims = [sum(a * b for a, b in zip(q, d)) for d in docs["vectors"]]
        assert sims.index(max(sims)) == i, (QUERIES[i], sims)

    with pytest.raises(httpx.HTTPError):                # this network really has no way out
        httpx.get("https://huggingface.co", timeout=5)


def test_a_swarms_tasks_are_embedded_by_the_model_and_indexed(api, fake):
    embeddings_ready(api)
    fake.configure(tasks_per_plan=2)
    swarm = api.create_swarm()
    api.transition(swarm["id"], "start")
    api.wait_verification(swarm["id"])

    def all_tasks_embedded():
        e = api.get("/embeddings")
        c = e["counts"]["tasks"]
        return e if e["status"]["state"] == "ready" and c["total"] >= 2 and c["indexed"] == c["total"] else None

    e = poll(all_tasks_embedded, timeout=60, desc="every task embedded by the built-in model")
    assert (e["status"]["active_model"], e["status"]["dim"]) == (MODEL, 768)
    assert e["counts"]["learnings"]["indexed"] == e["counts"]["learnings"]["total"]
    # one HNSW index per table, for the model in use
    assert all(len(names) == 1 for names in e["indexes"].values()), e["indexes"]
