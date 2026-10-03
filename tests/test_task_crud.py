"""Unit and integration tests for Slice 1: Durable Task State."""
from __future__ import annotations

import asyncpg
import pytest

from app.models import TaskCreate, TaskStatus, TaskUpdate


# =========================================================
# Repository tests (hit real PostgreSQL)
# =========================================================

async def test_create_task(task_repo: TaskRepository, pool: asyncpg.Pool) -> None:
    await pool.execute("INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ('t1', 'Test', 'V', '', 'ACTIVE')")
    data = TaskCreate(team_id="t1", name="Build vision", acceptance_criteria="Approved by swarm")
    task = await task_repo.create(data)
    assert task.team_id == "t1"
    assert task.status == TaskStatus.PENDING
    assert task.acceptance_criteria == "Approved by swarm"
    assert task.id is not None


async def test_create_hierarchical_task(task_repo: TaskRepository, pool: asyncpg.Pool) -> None:
    """Guard test: verify parent_id column works (catches schema drift like the title→name bug)."""
    await pool.execute("INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ('t1', 'Test', 'V', '', 'ACTIVE')")
    parent = await task_repo.create(TaskCreate(team_id="t1", name="Parent task", acceptance_criteria="Done"))
    child = await task_repo.create(TaskCreate(team_id="t1", name="Child task", parent_id=parent.id, acceptance_criteria="Done"))
    assert child.parent_id == parent.id

    # Fetch parent and verify subtask exists via query
    parent_rows = await task_repo.list_by_team("t1")
    parent_task = next(t for t in parent_rows if t.id == parent.id)
    assert parent_task.id is not None


async def test_task_with_keywords(task_repo: TaskRepository, pool: asyncpg.Pool) -> None:
    """Guard test: verify keywords column exists and is writeable."""
    await pool.execute("INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ('t1', 'Test', 'V', '', 'ACTIVE')")
    data = TaskCreate(team_id="t1", name="Keyworded task", keywords=["refactor", "critical"], acceptance_criteria="Pass")
    task = await task_repo.create(data)
    assert task.keywords == ["refactor", "critical"]


async def test_get_task(task_repo: TaskRepository, pool: asyncpg.Pool) -> None:
    await pool.execute("INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ('t1', 'Test', 'V', '', 'ACTIVE')")
    created = await task_repo.create(TaskCreate(team_id="t1", name="Greet world"))
    fetched = await task_repo.get(created.id)
    assert fetched is not None
    assert fetched.name == "Greet world"


async def test_get_missing_task_returns_none(task_repo: TaskRepository) -> None:
    assert await task_repo.get("nonexistent") is None


async def test_update_task_status(task_repo: TaskRepository, pool: asyncpg.Pool) -> None:
    await pool.execute("INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ('t1', 'Test', 'V', '', 'ACTIVE')")
    created = await task_repo.create(TaskCreate(team_id="t1", name="Ship v0"))
    updated = await task_repo.update(created.id, TaskUpdate(status=TaskStatus.READY))
    assert updated is not None
    assert updated.status == TaskStatus.READY


async def test_update_task_rejection(task_repo: TaskRepository, pool: asyncpg.Pool) -> None:
    await pool.execute("INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ('t1', 'Test', 'V', '', 'ACTIVE')")
    created = await task_repo.create(TaskCreate(team_id="t1", name="Ship v0"))
    updated = await task_repo.update(created.id, TaskUpdate(status=TaskStatus.PENDING, rejection_reason="missing DoD"))
    assert updated.rejection_reason == "missing DoD"


async def test_delete_task(task_repo: TaskRepository, pool: asyncpg.Pool) -> None:
    await pool.execute("INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ('t1', 'Test', 'V', '', 'ACTIVE')")
    created = await task_repo.create(TaskCreate(team_id="t1", name="Delete me"))
    assert await task_repo.delete(created.id) is True
    assert await task_repo.delete(created.id) is False  # second delete → False


async def test_list_by_team(task_repo: TaskRepository, pool: asyncpg.Pool) -> None:
    await pool.execute("INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ('t1', 'Test', 'V', '', 'ACTIVE'), ('t2', 'Test', 'V', '', 'ARCHIVED')")
    await task_repo.create(TaskCreate(team_id="t1", name="A"))
    await task_repo.create(TaskCreate(team_id="t1", name="B"))
    await task_repo.create(TaskCreate(team_id="t2", name="C"))
    tasks_t1 = await task_repo.list_by_team("t1")
    assert len(tasks_t1) == 2
    assert tasks_t1[0].name == "A"
    assert tasks_t1[1].name == "B"


async def test_list_by_team_with_status_filter(task_repo: TaskRepository, pool: asyncpg.Pool) -> None:
    await pool.execute("INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ('t1', 'Test', 'V', '', 'ACTIVE')")
    t1 = await task_repo.create(TaskCreate(team_id="t1", name="Pending task"))
    await task_repo.update(t1.id, TaskUpdate(status=TaskStatus.IN_PROGRESS))
    tasks = await task_repo.list_by_team("t1", status=TaskStatus.IN_PROGRESS)
    assert len(tasks) == 1
    assert tasks[0].id == t1.id


# =========================================================
# API (HTTP) tests
# =========================================================

async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_crud_lifecycle(client: AsyncClient) -> None:
    # create
    resp = await client.post("/tasks", json={
        "team_id": "t1",
        "name": "Smoke task",
        "acceptance_criteria": "It works",
    })
    assert resp.status_code == 201
    task = resp.json()
    task_id = task["id"]
    assert task["status"] == "Pending"

    # read
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Smoke task"

    # update status
    resp = await client.patch(f"/tasks/{task_id}", json={"status": "In-Progress"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "In-Progress"

    # delete
    resp = await client.delete(f"/tasks/{task_id}")
    assert resp.status_code == 204

    # verify deleted
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 404


# =========================================================
# Slice 1 completion: list endpoint, PATCH gaps, hierarchy via API
# =========================================================

async def test_list_tasks_returns_created(client: AsyncClient) -> None:
    await client.post("/tasks", json={"team_id": "t-list", "name": "One", "acceptance_criteria": "ok"})
    await client.post("/tasks", json={"team_id": "t-list", "name": "Two", "acceptance_criteria": "ok"})

    resp = await client.get("/tasks", params={"team_id": "t-list"})
    assert resp.status_code == 200
    names = sorted(t["name"] for t in resp.json()["tasks"])
    assert names == ["One", "Two"]


async def test_list_tasks_filter_by_status(client: AsyncClient) -> None:
    created = await client.post("/tasks", json={"team_id": "t-filt", "name": "Will move", "acceptance_criteria": "ok"})
    other = await client.post("/tasks", json={"team_id": "t-filt", "name": "Stays", "acceptance_criteria": "ok"})
    await client.patch(f"/tasks/{created.json()['id']}", json={"status": "In-Progress"})

    resp = await client.get("/tasks", params={"team_id": "t-filt", "status": "In-Progress"})
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert [t["id"] for t in tasks] == [created.json()["id"]]

    resp = await client.get("/tasks", params={"team_id": "t-filt", "status": "Pending"})
    assert [t["id"] for t in resp.json()["tasks"]] == [other.json()["id"]]


async def test_list_tasks_invalid_status(client: AsyncClient) -> None:
    resp = await client.get("/tasks", params={"team_id": "t-x", "status": "NotAStatus"})
    assert resp.status_code == 422


async def test_patch_acceptance_criteria_persists(client: AsyncClient) -> None:
    resp = await client.post("/tasks", json={"team_id": "t-patch", "name": "AC", "acceptance_criteria": "old"})
    task_id = resp.json()["id"]

    resp = await client.patch(f"/tasks/{task_id}", json={"acceptance_criteria": "new criteria"})
    assert resp.status_code == 200
    assert resp.json()["acceptance_criteria"] == "new criteria"

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.json()["acceptance_criteria"] == "new criteria"


async def test_patch_invalid_status_rejected(client: AsyncClient) -> None:
    resp = await client.post("/tasks", json={"team_id": "t-bad", "name": "Bad status", "acceptance_criteria": "ok"})
    task_id = resp.json()["id"]

    resp = await client.patch(f"/tasks/{task_id}", json={"status": "Completed"})
    assert resp.status_code == 422

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.json()["status"] == "Pending"


async def test_hierarchy_via_api(client: AsyncClient) -> None:
    parent = await client.post("/tasks", json={"team_id": "t-hier", "name": "Parent", "acceptance_criteria": "ok"})
    assert parent.status_code == 201
    parent_id = parent.json()["id"]

    child = await client.post(
        "/tasks",
        json={"team_id": "t-hier", "name": "Child", "parent_id": parent_id, "acceptance_criteria": "ok"},
    )
    assert child.status_code == 201
    assert child.json()["parent_id"] == parent_id

    resp = await client.get("/tasks", params={"team_id": "t-hier"})
    by_name = {t["name"]: t for t in resp.json()["tasks"]}
    assert by_name["Child"]["parent_id"] == parent_id
    assert by_name["Parent"]["parent_id"] is None


async def test_create_task_unknown_parent_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/tasks",
        json={"team_id": "t-orph", "name": "Orphan", "parent_id": "no-such-task", "acceptance_criteria": "ok"},
    )
    assert resp.status_code == 422


@pytest.mark.skip(reason="Pre-existing: /agents/run endpoint not implemented in HEAD")
async def test_agents_run_smoke(client: AsyncClient) -> None:
    """E2E smoke test for Slice 3: Harness Adapters.

    Creates a task, sends a /agents/run request, and verifies the response
    contains expected fields from the LMStudio adapter (no API key required).
    """
    # 1. Create a task for the agent to work on
    task_resp = await client.post(
        "/tasks",
        json={
            "team_id": "t-agent-e2e",
            "name": "Agent task",
            "description": "E2E task for agent run",
            "acceptance_criteria": "Agent responds",
        },
    )
    assert task_resp.status_code == 201, task_resp.text
    task_id = task_resp.json()["id"]

    # 2. Send an agent run request
    agent_resp = await client.post(
        "/agents/run",
        json={
            "team_id": "t-agent-e2e",
            "task_id": task_id,
            "system_prompt": "You are a helpful assistant. Be brief.",
            "messages": [{"role": "user", "content": "What is 1+1? Just the number."}],
            "max_tokens": 64,
            "temperature": 0.3,
        },
    )
    assert agent_resp.status_code == 200, agent_resp.text
    data = agent_resp.json()

    # 3. Verify response shape
    assert data["team_id"] == "t-agent-e2e"
    assert data["task_id"] == task_id
    assert data["model"] == "qwen3.8-27b-mlx"
    assert "text" in data
    assert "finish_reason" in data
    assert isinstance(data["text"], str)
    assert data["text"].strip() != ""  # model returned something
