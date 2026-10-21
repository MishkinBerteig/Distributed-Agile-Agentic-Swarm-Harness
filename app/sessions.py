"""Agent session orchestration (Slice 5.1).

The coordinator lives in Docker and cannot spawn host processes; the runner
(app.runner) is the bridge to the device where harness CLIs are installed. This
module decides *what* launches when a swarm goes live — one headless session per
team member role, each carrying the deterministic Vision→Mission→Role prompt —
and keeps PostgreSQL's view of those sessions honest via a reconcile loop.

Every runner call degrades to None/[]: a missing or dead runner must never fail
a lifecycle transition or an endpoint. The swarm still starts; its roster simply
shows why nothing launched.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

import httpx

from app.config import Settings
from app.models import Swarm, SwarmStatus, Team
from app.prompts import composeAgentRolePrompt
from app.repo import AgentSessionRepository, SwarmRepository, TeamRepository

log = logging.getLogger("daash.sessions")

#: (role, harness). 5.1.0 runs every member on OpenClaude; heterogeneous teams
# (5.1.4) are a change to the harness column here — nothing else.
ROLES: tuple[tuple[str, str], ...] = (
    ("Mission Judgement", "openclaude"),
    ("Quality Judgement", "openclaude"),
    ("Mission Delivery", "openclaude"),
)

WAKE_TURN = "Acknowledge your role by replying exactly: READY {role}."


class RunnerClient:
    """Thin HTTP client for the host runner; never raises to callers."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        headers = {}
        if settings.RUNNER_TOKEN:
            headers["Authorization"] = f"Bearer {settings.RUNNER_TOKEN}"
        self._client = httpx.AsyncClient(
            base_url=settings.RUNNER_URL.rstrip("/") or "http://runner.invalid",
            timeout=settings.RUNNER_TIMEOUT_SECONDS, headers=headers, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def enabled(self) -> bool:
        return bool(self._settings.RUNNER_URL)

    async def _get(self, path: str) -> Any | None:
        try:
            r = await self._client.get(path)
            if r.status_code == 200:
                return r.json()
            log.warning("runner GET %s -> %s", path, r.status_code)
        except httpx.HTTPError as exc:
            log.debug("runner unreachable (%s): %s", path, exc)
        return None

    async def spawn(self, spec: dict) -> Optional[dict]:
        try:
            r = await self._client.post("/sessions", json=spec)
            if r.status_code in (200, 201):
                return r.json()
            log.warning("runner POST /sessions -> %s: %s", r.status_code, r.text[:300])
        except httpx.HTTPError as exc:
            log.info("runner unreachable for spawn: %s", exc)
        return None

    async def list_sessions(self) -> Optional[list[dict]]:
        data = await self._get("/sessions")
        return data if isinstance(data, list) else None

    async def get_session(self, session_id: str) -> Optional[dict]:
        data = await self._get(f"/sessions/{session_id}")
        return data if isinstance(data, dict) else None

    async def send_turn(self, session_id: str, text: str) -> bool:
        try:
            r = await self._client.post(f"/sessions/{session_id}/turns", json={"text": text})
            return r.status_code == 202
        except httpx.HTTPError as exc:
            log.debug("runner send_turn failed: %s", exc)
            return False

    async def terminate(self, session_id: str) -> bool:
        try:
            await self._client.delete(f"/sessions/{session_id}")
            return True
        except httpx.HTTPError as exc:
            log.debug("runner terminate failed for %s: %s", session_id, exc)
            return False


class SessionOrchestrator:
    def __init__(self, pool, client: RunnerClient, settings: Settings) -> None:
        self._pool = pool
        self._client = client
        self._settings = settings
        self.swarms = SwarmRepository(pool)
        self.teams = TeamRepository(pool)
        self.sessions = AgentSessionRepository(pool)

    # -- lifecycle side effects ---------------------------------------------

    async def launch_swarm(self, swarm: Swarm) -> None:
        """Spawn one live session per team member role. Never raises."""
        if not self._client.enabled:
            log.info("runner disabled (RUNNER_URL unset); no sessions launched for %s", swarm.id)
            return
        teams = await self.teams.list(swarm_id=swarm.id)
        results = await asyncio.gather(
            *(self._launch_team(swarm, t) for t in teams), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):  # defensive: _launch_team swallows already
                log.exception("unexpected launch error: %s", r)

    async def _launch_team(self, swarm: Swarm, team: Team) -> None:
        specs = []
        for role, harness in ROLES:
            session_id = str(uuid.uuid4())
            prompt = composeAgentRolePrompt(
                swarm.vision_statement, team.mission_statement, role,
                team_name=team.name, coordinator_url=self._settings.COORDINATOR_BASE_URL,
                team_id=team.id)
            try:
                await self.sessions.create(id=session_id, swarm_id=swarm.id,
                                           team_id=team.id, role=role, harness=harness,
                                           session_state="launching")
            except Exception as exc:  # noqa: BLE001 — row must exist before spawn
                log.error("cannot record session for %s/%s: %s", team.id, role, exc)
                continue
            spec = {"session_id": session_id, "swarm_id": swarm.id, "team_id": team.id,
                    "role": role, "harness": harness, "system_prompt": prompt}
            specs.append((session_id, role, spec))

        spawn_results = await asyncio.gather(
            *(self._client.spawn(spec) for _, _, spec in specs), return_exceptions=True)

        for (sid, role, _spec), res in zip(specs, spawn_results):
            if isinstance(res, Exception) or res is None:
                reason = ("runner unreachable — start it with "
                          "`DAASH_RUNNER_TOKEN=... python3 -m app.runner`"
                          if not isinstance(res, Exception) else str(res))
                await self.sessions.update_state(sid, "failed", error=reason[:500])
                continue
            state = res.get("state", "launching")
            await self.sessions.update_state(
                sid, state if state != "stopped" else "failed",
                model=res.get("model"), error=res.get("error"))
            # Wake turn forces the CLI's init event so provider/model resolve.
            asyncio.create_task(self._client.send_turn(sid, WAKE_TURN.format(role=role)))

    async def stop_swarm(self, swarm_id: str) -> None:
        """Terminate every session of a swarm. Never raises."""
        rows = await self.sessions.list_for_swarm(swarm_id)
        live = [r for r in rows if r.session_state in ("launching", "live")]
        await asyncio.gather(*(self._client.terminate(r.id) for r in live),
                             return_exceptions=True)
        await self.sessions.mark_stopped_for_swarm(swarm_id)

    # -- reconciliation -------------------------------------------------------

    async def reconcile_swarm(self, swarm_id: str) -> None:
        """Sync DB session rows against the runner's live view."""
        if not self._client.enabled:
            return
        listed = await self._client.list_sessions()
        if listed is None:
            for row in await self.sessions.list_for_swarm(swarm_id):
                if row.session_state == "live":
                    await self.sessions.update_state(
                        row.id, "failed", error="runner unreachable — session state unknown")
            return
        by_id = {d.get("session_id"): d for d in listed}
        for row in await self.sessions.list_for_swarm(swarm_id):
            if row.session_state in ("stopped",):
                continue
            info = by_id.get(row.id)
            if info is None:
                await self.sessions.update_state(
                    row.id, "failed", error="session not found on runner (runner restarted?)")
                continue
            state = info.get("state", row.session_state)
            model = info.get("model") or row.model
            turns = info.get("turns", row.turns)
            hsess = info.get("harness_session_id") or row.harness_session_id
            if state in ("live", "launching"):
                await self.sessions.update_state(row.id, state, model=model, turns=turns,
                                                 harness_session_id=hsess)
            else:  # stopped/failed on the runner
                err = info.get("error") or ("session stopped by runner" if state == "stopped" else None)
                await self.sessions.update_state(row.id, "failed" if state == "failed" else state,
                                                 error=err, model=model, turns=turns, harness_session_id=hsess)

    async def reconcile_all(self) -> None:
        swarms = await SwarmRepository(self._pool).list()
        terminal = SwarmStatus.terminal()
        for s in swarms:
            if s.lifecycle_state in terminal:
                continue
            try:
                await self.reconcile_swarm(s.id)
            except Exception as exc:  # noqa: BLE001 — loop must not die
                log.warning("reconcile failed for swarm %s: %s", s.id, exc)

    async def run_loop(self) -> None:
        """Background reconcile task (only started when the runner is configured)."""
        interval = self._settings.SESSION_RECONCILE_SECONDS
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reconcile_all()
            except Exception as exc:  # noqa: BLE001
                log.warning("reconcile loop iteration failed: %s", exc)
