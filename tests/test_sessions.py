"""Slice 5.1: orchestrator tests for launched agent-session lifecycle.

Exercises SessionOrchestrator against a fake runner client so the suite
stays isolated from any host process or harness CLI. Each test seeds its own
Swarm + Team row via the real repositories, giving agent-session writes valid
foreign keys. Uses the conftest ``pool`` fixture; asyncio_mode=auto, no markers.

Covered paths:
  - launch_swarm seeds one session row per member role with its state/model.
  - a successful spawn writes the runner-reported model + status into each row.
  - an individual failed/missing spawn marks only that report "failed" while the
    rest stay healthy (no exception, no total launch failure).
  - stop_swarm terminates all live sessions and flushes them to "stopped".
  - reconcile diffs DB against a runner snapshot: fresh model/turns flow in, and
    an active session the runner no longer reports is marked failed.
"""
from __future__ import annotations

import uuid

from app.config import Settings
from app.models import SwarmStatus, Team  # noqa: F401 (typing references)
from app.repo import AgentSessionRepository, SwarmRepository, TeamRepository
from app.sessions import ROLES, SessionOrchestrator

_DEFAULT_STATE = "launching"


def _seed_swarm(pool, vision: str = "to ship value"):
    return SwarmRepository(pool).create(f"swarm-{uuid.uuid4().hex[:8]}", vision)


def _seed_team(pool, swarm_id: str):
    return TeamRepository(pool).create(swarm_id, f"team-{uuid.uuid4().hex[:8]}", "to do the thing")


class FakeClient:
    """RunnerClient stand-in with recorded calls and a controllable state map."""

    def __init__(self, *, default_state: str = _DEFAULT_STATE):
        self.default_state = default_state
        self.enabled = True                      # sync flag gating session branch logic upstream.
        self.registry: dict[str, dict] = {}      # {session_id: status} mirror of runner tail buffer.
        self.spawn_calls: list[dict] = []
        self.terminate_calls: list[str] = []
        self.sent_turns: list[tuple[str, str]] = []
        self.spawn_hook = None                   # async(spec) -> dict|None override for negatives.

    @property
    def live(self):                             # test helper: current registry snapshot count.
        return len(self.registry)

    async def spawn(self, spec: dict):
        sid = spec.get("session_id") or f"auto-{len(self.registry)+1}"
        if self.spawn_hook is not None:         # per-test failure injection cleanly.
            return await self.spawn_hook(spec)  # None mirrors a lost/at-risk spawn on the runner.
        res = {"session_id": sid, "state": self.default_state,
               "model": spec.get("harness"), "error": None}
        # persist record so list_sessions reveals this session after its round.
        self.registry[sid] = {**res, "turns": 0}
        self.spawn_calls.append(spec)
        return res

    async def list_sessions(self):
        out = []
        for sid, rec in self.registry.items():  # snapshot the status tail buffer.
            out.append({"session_id": sid, "state": rec.get("state", self.default_state),
                        "model": rec.get("model"), "turns": rec.get("turns", 0),
                        "error": rec.get("error"), "_missing_error_tag": False})
        return out

    async def send_turn(self, session_id: str, text: str) -> bool:
        self.sent_turns.append((session_id, text))  # capture each wake/spawn turn issued by orchestrator.
        return True                               # mirrors runner acknowledging a submitted user message.

    async def terminate(self, session_id: str) -> bool:
        self.terminate_calls.append(session_id)   # emulate runner killing process + closing pipes.
        return True


def _orchestrator(pool, client):
    """Construct a real SessionOrchestrator (it builds its own repos from pool)."""
    return SessionOrchestrator(pool=pool, client=client, settings=Settings())


# --- tests -----------------------------------------------------------------

class TestLaunchSwarm:
    async def test_seeds_one_session_per_role(self, pool):
        swarm = await _seed_swarm(pool)
        client = FakeClient()

        # Seed one team so the roster has members to spawn.
        await _seed_team(pool, swarm.id)

        orch = _orchestrator(pool, client)
        # ROLES is the production tuple; launch must create exactly one row each.
        await orch.launch_swarm(swarm)

        rows = await AgentSessionRepository(pool).list_for_swarm(swarm.id)
        assert len(rows) == len(ROLES)
        roles = {r.role for r in rows}
        assert set(r[0] for r in ROLES).issubset(roles)

    async def test_successful_spawn_records_model(self, pool):
        swarm = await _seed_swarm(pool)
        client = FakeClient()
        await _seed_team(pool, swarm.id)

        orch = _orchestrator(pool, client)
        await orch.launch_swarm(swarm)

        rows = await AgentSessionRepository(pool).list_for_swarm(swarm.id)
        # default_state launching + harness name recorded as model.
        for r in rows:
            assert r.session_state == "launching"
            assert r.model  # spec harness resolves into model name

    async def test_failed_spawn_marks_only_that_row(self, pool):
        swarm = await _seed_swarm(pool)
        client = FakeClient()

        sess_ids = []  # create sessions like _launch_team does, then test hook.
        await _seed_team(pool, swarm.id)

        # Simulate runner losing exactly one session: first returned spawn fails.
        calls_made = {"n": 0}

        async def partial_fail(spec):
            if calls_made["n"] != 0:
                return {"session_id": spec["session_id"], "state": "launching",
                        "model": spec.get("harness"), "error": None}
            calls_made["n"] += 1
            return None

        client.spawn_hook = partial_fail
        orch = _orchestrator(pool, client)
        await orch.launch_swarm(swarm)

        rows = await AgentSessionRepository(pool).list_for_swarm(swarm.id)
        states = [r.session_state for r in rows]
        # Some failed + some remaining launching (mixed per-role outcome).
        assert "failed" in states
        assert any(s == "launching" for s in states)


class TestStopSwarm:
    async def test_terminates_live_sessions_and_marks_stopped(self, pool):
        swarm = await _seed_swarm(pool)
        client = FakeClient()
        team = await _seed_team(pool, swarm.id)

        orch = _orchestrator(pool, client)
        await orch.launch_swarm(swarm)

        rows = await AgentSessionRepository(pool).list_for_swarm(swarm.id)
        for r in rows:
            # promote to live so stop_swarm treats them as active.
            await AgentSessionRepository(pool).update_state(r.id, "live")

        all_rows = await AgentSessionRepository(pool).list_for_swarm(swarm.id)
        rows_live = [r for r in all_rows if r.session_state == "live"]
        assert rows_live, "expected live rows before stop"

        await orch.stop_swarm(swarm.id)

        # runner TERMINATE called for each live session
        assert len(client.terminate_calls) == len(rows_live)

        # rows now marked stopped (no longer live/launching)
        after = await AgentSessionRepository(pool).list_for_swarm(swarm.id)
        assert all(r.session_state in ("stopped", "failed") for r in after)

    async def test_graceful_when_no_live_rows(self, pool):
        """stop_swarm on a swarm with no live sessions is a safe no-op for calls."""
        swarm = await _seed_swarm(pool)
        client = FakeClient()
        orch = _orchestrator(pool, client)

        # No sessions launched → no terminate calls; must not hang or raise.
        await orch.stop_swarm(swarm.id)
        assert client.terminate_calls == []


class TestReconcile:
    async def _launch(self, pool):
        """Helper to get a swarm with fresh launching sessions."""
        swarm = await _seed_swarm(pool)
        client = FakeClient()
        team = await _seed_team(pool, swarm.id)
        orch = _orchestrator(pool, client)
        await orch.launch_swarm(swarm)
        return swarm, client

    async def test_new_model_and_turns_flow_in(self, pool):
        swarm, client = await self._launch(pool)
        # mutate one session's profile on the runner side as init events resolve.
        sid = next(iter(client.registry))
        client.registry[sid] = {"session_id": sid, "state": "live",
                                 "model": "resolved-model-x", "turns": 7, "error": None}

        await _orchestrator(pool, client).reconcile_swarm(swarm.id)

        row = next(r for r in await AgentSessionRepository(pool).list_for_swarm(swarm.id) if r.id == sid)
        assert row.session_state == "live"
        assert row.model == "resolved-model-x"
        assert row.turns == 7

    async def test_missing_runner_session_marked_failed(self, pool):
        swarm, client = await self._launch(pool)
        # Drop one session from the runner's view; it was active (live) before.

        # First, promote all to live so missing detection fires for active rows only.
        sess_rows = await AgentSessionRepository(pool).list_for_swarm(swarm.id)
        for r in sess_rows[:-1]:  # keep at least one known to avoid re-list confusion.
            await AgentSessionRepository(pool).update_state(r.id, "live")
        missing = sess_rows[-1]  # drop from runner snapshot below.
        client.registry.pop(missing.id, None)

        orch = _orchestrator(pool, client)
        await orch.reconcile_swarm(swarm.id)

        rows_now = {r.id: r for r in await AgentSessionRepository(pool).list_for_swarm(swarm.id)}
        assert rows_now[missing.id].session_state == "failed"

