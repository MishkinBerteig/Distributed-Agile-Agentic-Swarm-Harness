"""Slice 1.5: DAASHboard backend — list swarms and create a swarm."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _clean(pool):
    await pool.execute("TRUNCATE transcripts, memory_entries, tasks, teams CASCADE")


@pytest.mark.asyncio
async def test_create_swarm(client):
    resp = await client.post(
        "/swarms",
        json={"name": "Alpha Swarm", "vision_statement": "ship daily", "mission_statement": "verify everything"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"]
    assert body["name"] == "Alpha Swarm"
    assert body["vision_statement"] == "ship daily"
    assert body["mission_statement"] == "verify everything"


@pytest.mark.asyncio
async def test_create_swarm_requires_name(client):
    resp = await client.post("/swarms", json={"name": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_teams_empty(client):
    resp = await client.get("/teams")
    assert resp.status_code == 200
    assert resp.json() == {"teams": []}


@pytest.mark.asyncio
async def test_swarm_appears_in_teams_and_hosts_tasks(client, pool):
    created = await client.post("/swarms", json={"name": "Bravo Swarm", "vision_statement": "v"})
    team_id = created.json()["id"]

    listed = await client.get("/teams")
    ids = [t["id"] for t in listed.json()["teams"]]
    assert team_id in ids

    task = await client.post(
        "/tasks", json={"team_id": team_id, "name": "Inspect me", "description": "d", "acceptance_criteria": "c"}
    )
    assert task.status_code == 201, task.text
    parent = task.json()["id"]

    child = await client.post(
        "/tasks", json={"team_id": team_id, "name": "Subtask", "parent_id": parent}
    )
    assert child.status_code == 201, child.text

    tasks = (await client.get("/tasks", params={"team_id": team_id})).json()["tasks"]
    assert {t["id"] for t in tasks} == {parent, child.json()["id"]}
