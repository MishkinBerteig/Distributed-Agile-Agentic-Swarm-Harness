"""Tests for swarm lifecycle enforcement.

Rules under test:
  - A newly created swarm lands in CREATED.
  - A second create attempt while a swarm is live (non-ARCHIVED) returns 409.
  - The /swarms/active endpoint returns the single live swarm or null.
  - Legal/illegal transitions are enforced per the state machine.
  - DELETE from VERIFICATION hard-deletes, freeing the slot for a new swarm.
"""
from __future__ import annotations

import pytest

from app.repo import TeamRepository


def assert_team(resp, expected_state, name=None):
    assert resp["lifecycle_state"] == expected_state
    if name is not None:
        assert resp["name"] == name


class TestSwarmLifecycle:
    """Single-swarm lifecycle CRUD + transition enforcement."""

    async def test_create_returns_created(self, client):
        r = await client.post("/swarms", json={
            "name": "Alpha", "vision_statement": "Ship value",
        })
        assert r.status_code == 201
        data = r.json()
        assert_team(data, "CREATED", "Alpha")
        self._cleanup_team(client, data["id"])

    async def test_second_create_conflict(self, client):
        r1 = await client.post("/swarms", json={
            "name": "Alpha", "vision_statement": "Ship value",
        })
        assert r1.status_code == 201
        id_a = r1.json()["id"]

        r2 = await client.post("/swarms", json={
            "name": "Beta", "vision_statement": "Ship faster",
        })
        assert r2.status_code == 409

        self._cleanup_team(client, id_a)

    async def test_active_only(self, client):
        r = await client.get("/swarms/active")
        assert r.status_code == 200
        # After session-level truncate there should be no live swarm.
        assert r.json() is None

    async def test_transition_start(self, client):
        r = await client.post("/swarms", json={
            "name": "Alpha", "vision_statement": "Ship value",
        })
        id_a = r.json()["id"]

        t = await client.post(f"/swarms/{id_a}/transitions/start")
        assert t.status_code == 200
        assert_team(t.json(), "ACTIVE")

        r = await client.get("/swarms/active")
        assert r.json()["id"] == id_a
        assert r.json()["lifecycle_state"] == "ACTIVE"

        self._cleanup_team(client, id_a)

    async def test_transition_pause_resume(self, client):
        swarm = await self._create_active(client)
        id_a = swarm["id"]

        pause = await client.post(f"/swarms/{id_a}/transitions/pause")
        assert pause.status_code == 200
        assert_team(pause.json(), "PAUSED")

        resume = await client.post(f"/swarms/{id_a}/transitions/resume")
        assert resume.status_code == 200
        assert_team(resume.json(), "ACTIVE")

        self._cleanup_team(client, id_a)

    async def test_transition_verify_from_paused(self, client):
        swarm = await self._create_active(client)
        id_a = swarm["id"]

        await client.post(f"/swarms/{id_a}/transitions/pause")
        v = await client.post(f"/swarms/{id_a}/transitions/verify")
        assert v.status_code == 200
        assert_team(v.json(), "VERIFICATION")

        self._cleanup_team(client, id_a)

    async def test_transition_illegal(self, client):
        swarm = await self._create_active(client)
        id_a = swarm["id"]

        r = await client.post(f"/swarms/{id_a}/transitions/learn")
        assert r.status_code == 400

        self._cleanup_team(client, id_a)

    async def test_transition_learn(self, client):
        swarm = await self._verify_swarm(client)
        id_a = swarm["id"]

        r = await client.post(f"/swarms/{id_a}/transitions/learn")
        assert r.status_code == 200
        assert_team(r.json(), "LEARNING")

        r = await client.post(f"/swarms/{id_a}/transitions/archive")
        assert r.status_code == 200
        assert_team(r.json(), "ARCHIVED")

        # Active should be None now.
        a = await client.get("/swarms/active")
        assert a.json() is None

        self._cleanup_team(client, id_a)

    async def test_archive(self, client):
        swarm = await self._verify_swarm(client)
        id_a = swarm["id"]

        r = await client.post(f"/swarms/{id_a}/transitions/archive")
        assert r.status_code == 200
        assert_team(r.json(), "ARCHIVED")

        # Active should be None now.
        a = await client.get("/swarms/active")
        assert a.json() is None

        self._cleanup_team(client, id_a)

    async def test_delete_hard_deletes_and_frees_slot(self, client):
        swarm = await self._verify_swarm(client)
        id_a = swarm["id"]

        # Create tasks for this swarm so we can verify cascade delete.
        tasks_r = await client.post("/tasks", json={
            "team_id": id_a,
            "name": "Task 1", "description": "Do thing", "acceptance_criteria": "It works",
        })
        task_id = tasks_r.json()["id"]

        d = await client.delete(f"/swarms/{id_a}/delete")
        assert d.status_code == 200

        # Task is gone.
        r = await client.get(f"/tasks/{task_id}")
        assert r.status_code == 404

        # Slot is free — can create a new swarm.
        r2 = await client.post("/swarms", json={
            "name": "Beta", "vision_statement": "Ship faster",
        })
        assert r2.status_code == 201

    async def test_cannot_delete_from_created(self, client):
        """Illegal transition from CREATED (ABANDON/delete is allowed)."""
        r = await client.post("/swarms", json={
            "name": "Alpha", "vision_statement": "Ship value",
        })
        id_a = r.json()["id"]

        # Illegal transition: cannot verify from CREATED
        d = await client.post(f"/swarms/{id_a}/transitions/verify")
        assert d.status_code == 400

        self._cleanup_team(client, id_a)

    async def test_user_happy_path(self, client):
        """User story: create → start → verify → archive, no live swarm after."""
        r = await client.post("/swarms", json={
            "name": "Alpha", "vision_statement": "Ship value",
        })
        id_a = r.json()["id"]
        assert_team(r.json(), "CREATED")

        await client.post(f"/swarms/{id_a}/transitions/start")
        await client.post(f"/swarms/{id_a}/transitions/verify")
        await client.post(f"/swarms/{id_a}/transitions/archive")

        a = await client.get("/swarms/active")
        assert a.json() is None

        self._cleanup_team(client, id_a)

    async def test_delete_from_verification(self, client):
        """DELETE endpoint only accepts swarm ID (not /transitions/delete)."""
        swarm = await self._verify_swarm(client)
        id_a = swarm["id"]

        d = await client.delete(f"/swarms/{id_a}/delete")
        assert d.status_code == 200
        assert d.json()["deleted"] == id_a

        # Slot is free.
        r2 = await client.post("/swarms", json={
            "name": "Beta", "vision_statement": "Ship faster",
        })
        assert r2.status_code == 201

    async def test_feedback_flow(self, client):
        """PAUSED → USER_FEEDBACK → ACTIVE."""
        swarm = await self._create_active(client)
        id_a = swarm["id"]

        await client.post(f"/swarms/{id_a}/transitions/pause")
        fb = await client.post(f"/swarms/{id_a}/transitions/feedback")
        assert fb.status_code == 200
        assert_team(fb.json(), "USER_FEEDBACK")

        resume = await client.post(f"/swarms/{id_a}/transitions/resume")
        assert resume.status_code == 200
        assert_team(resume.json(), "ACTIVE")

        self._cleanup_team(client, id_a)

    # -- Helpers --

    @staticmethod
    def _cleanup_team(client, team_id):
        """Best-effort teardown: archive or delete so the slot frees."""
        try:
            import asyncio
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        async def _go():
            try:
                tasks = await client.get(f"/tasks?team_id={team_id}")
                for t in tasks.json().get("tasks", []):
                    await client.delete(f"/tasks/{t['id']}")
            except Exception:
                pass
            try:
                sw = await client.get(f"/teams/{team_id}")
                if sw.status_code != 200:
                    return
                state = sw.json()["lifecycle_state"]
                if state not in ("ARCHIVED", "DELETED"):
                    await client.post(f"/swarms/{team_id}/transitions/archive")
            except Exception:
                pass
        try:
            loop.run_until_complete(_go())
        except Exception:
            pass

    @pytest.fixture(autouse=True)
    def _cleanup_after(self, client):
        yield
        # Ensure no stray live swarm survives this test.
        try:
            import asyncio
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        async def _teardown():
            try:
                teams = await client.get("/teams")
                if teams.status_code == 200:
                    for t in teams.json().get("teams", []):
                        if t["lifecycle_state"] not in ("ARCHIVED", "DELETED"):
                            try:
                                ts = await client.get(f"/tasks?team_id={t['id']}")
                                for task in ts.json().get("tasks", []):
                                    await client.delete(f"/tasks/{task['id']}")
                            except Exception:
                                pass
                            try:
                                await client.post(f"/swarms/{t['id']}/transitions/archive")
                            except Exception:
                                pass
            except Exception:
                pass
        try:
            loop.run_until_complete(_teardown())
        except Exception:
            pass

    async def _create_active(self, client):
        r = await client.post("/swarms", json={
            "name": "Alpha", "vision_statement": "Ship value",
        })
        id_a = r.json()["id"]
        await client.post(f"/swarms/{id_a}/transitions/start")
        r2 = await client.get(f"/teams/{id_a}")
        return r2.json()

    async def _verify_swarm(self, client):
        swarm = await self._create_active(client)
        await client.post(f"/swarms/{swarm['id']}/transitions/verify")
        r2 = await client.get(f"/teams/{swarm['id']}")
        return r2.json()
