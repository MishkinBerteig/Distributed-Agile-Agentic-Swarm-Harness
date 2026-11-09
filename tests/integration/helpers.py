"""Helpers that drive the real Engine in-process against the fake LLM (docs/DESIGN-0.2.md A.2).

`Harness` builds an `Engine`, sets its `pool`/`bus`, and gives it `Jobs` whose `LLMClient`
talks to the fake over `httpx.MockTransport` (no network). It never modifies app code: the
engine is stepped with ``await engine.tick()`` then ``await asyncio.gather(*engine.running)``.
Engine cooldowns use an injectable `FakeClock`, so back-off tests do not wait.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Union

import httpx

from app import api_helpers as H
from app import policy, services
from app.embeddings import build_embedder
from app.engine import Cooldowns, Deferrals, Engine
from app.jobs import Jobs
from app.llm import LLMClient
from app.models import (CriterionCreate, CheckKind, Swarm, SwarmConfig, Task, TaskCreate,
                        TaskStatus, Team)
from app.repo import (AgentRepo, CallRepo, EventRepo, ProposalRepo, SwarmRepo, TaskRepo,
                      TeamRepo, VerdictRepo)


class FakeClock:
    """Monotonic clock for engine cooldowns; advance it instead of sleeping."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


async def no_sleep(_seconds: float) -> None:
    """LLMClient retry back-off without the wait."""
    return None


def make_llm(fake, endpoint: str = "http://fake-llm:9100", **kwargs: Any) -> LLMClient:
    return LLMClient(endpoint, transport=httpx.MockTransport(fake.httpx_handler),
                     sleep=no_sleep, **kwargs)


def swarm_config(delivery_agents: int = 1, **limits: Any) -> SwarmConfig:
    """A SwarmConfig for tests: roomy concurrency unless overridden."""
    cfg = SwarmConfig()
    cfg.delivery_agents_per_team = delivery_agents
    cfg.limits.max_simultaneous_agents = 8
    for key, value in limits.items():
        setattr(cfg.limits, key, value)
    return cfg


def default_criteria(artifact: str) -> list[CriterionCreate]:
    """Same shape as the fake LLM's plan: three deterministic checks and a rubric."""
    return [
        CriterionCreate(text=f"{artifact} exists", check_kind=CheckKind.FILE_EXISTS,
                        check_path=artifact),
        CriterionCreate(text=f"{artifact} has at least 20 words", check_kind=CheckKind.MIN_WORDS,
                        check_path=artifact, check_arg="20"),
        CriterionCreate(text=f"{artifact} has an Overview", check_kind=CheckKind.FILE_CONTAINS,
                        check_path=artifact, check_arg="Overview"),
        CriterionCreate(text="The content is clear and complete"),
    ]


class StuckError(AssertionError):
    pass


Predicate = Callable[[], Union[bool, Awaitable[bool]]]


class Harness:
    def __init__(self, pool, bus, fake, settings, clock: FakeClock) -> None:
        self.pool, self.bus, self.fake, self.settings, self.clock = pool, bus, fake, settings, clock
        self.swarms, self.teams = SwarmRepo(pool), TeamRepo(pool)
        self.agents, self.tasks = AgentRepo(pool), TaskRepo(pool)
        self.events, self.proposals = EventRepo(pool), ProposalRepo(pool)
        self.verdicts, self.calls = VerdictRepo(pool), CallRepo(pool)
        self.embedder = build_embedder("none", False)
        self.llm = make_llm(fake, settings.LLM_ENDPOINT)
        self.engine = self.new_engine()
        self.history: list[list[str]] = []   # action kinds per step
        self.swarm_id: Optional[str] = None

    # -- construction ---------------------------------------------------------------

    def new_engine(self, llm: Optional[LLMClient] = None) -> Engine:
        engine = Engine(self.settings)
        engine.pool = self.pool
        engine.bus = self.bus
        engine.cooldowns = Cooldowns(clock=self.clock)
        engine.deferrals = Deferrals(clock=self.clock)
        engine.jobs = Jobs(self.pool, llm or self.llm, self.bus, self.embedder, self.settings)
        return engine

    async def close(self) -> None:
        for task in list(self.engine.running.values()):
            task.cancel()
        if self.engine.running:
            await asyncio.gather(*list(self.engine.running.values()), return_exceptions=True)

    # -- seeding (same effect as the API's POST /swarms and the user's transitions) -----------

    async def create_swarm(self, vision: str = "Write a short guide to compost.",
                           name: str = "Test swarm", teams: Iterable[tuple[str, str]] = (),
                           config: Optional[SwarmConfig] = None, dod: Optional[str] = None,
                           start: bool = False) -> Swarm:
        swarm = await self.swarms.create(name, vision, dod, config or swarm_config())
        services.ensure_workspace(self.settings, swarm.id)
        for tname, mission in teams:
            await services.create_team(self.pool, swarm, tname, mission)
        await services.ensure_coordinator(self.pool, swarm)
        self.swarm_id = swarm.id
        if start:
            await self.transition("start")
        return await self.live()

    async def transition(self, action: str) -> Swarm:
        swarm = await self.live()
        new_state = policy.transition(swarm.lifecycle_state, action)
        fields = H.transition_fields(action, new_state, swarm.started_at,
                                     datetime.now(timezone.utc))
        updated = await self.swarms.update(swarm.id, **fields)
        await self.events.add(swarm.id, "swarm", actor="user",
                              detail=H.transition_detail(action, swarm.lifecycle_state, new_state))
        return updated

    async def live(self) -> Swarm:
        swarm = await self.swarms.get(self.swarm_id) if self.swarm_id else None
        assert swarm is not None, "no swarm created yet"
        return swarm

    async def seed_team(self, name: str = "Team A", delivery_agents: Optional[int] = None,
                        standards: bool = True) -> Team:
        """Add a team with its roster (and, by default, its Quality Judgement standards)."""
        swarm = await self.live()
        if swarm.definition_of_done is None:
            swarm = await self.swarms.update(swarm.id, definition_of_done="- The guide exists")
        team = await services.create_team(self.pool, swarm, name,
                                          f"To advance the Vision, {name} writes its part.")
        if standards:
            team = await self.teams.update(
                team.id, definition_of_ready="- The task names the file it changes",
                definition_of_done="- Every file is complete")
        return team

    async def add_task(self, team: Team, name: str, *, artifact: Optional[str] = None,
                       status: str = "Pending", depends_on: Iterable[str] = (),
                       criteria: Optional[list[CriterionCreate]] = None,
                       **fields: Any) -> Task:
        artifact = artifact if artifact is not None else f"{name.lower().replace(' ', '-')}.md"
        task = await self.tasks.create(
            team.swarm_id,
            TaskCreate(team_id=team.id, name=name, artifact=artifact,
                       depends_on=list(depends_on),
                       acceptance_criteria=criteria if criteria is not None
                       else default_criteria(artifact)),
            created_by="user")
        updates = dict(fields)
        if status != TaskStatus.PENDING.value:
            updates["status"] = status
        if updates:
            task = await self.tasks.update(task.id, **updates)
        return task

    def workspace_file(self, rel: str) -> str:
        """Text of a file the agents wrote into the swarm workspace."""
        return (Path(self.settings.WORKSPACE_ROOT) / self.swarm_id / rel).read_text()

    # -- queries -----------------------------------------------------------------------

    async def swarm(self) -> Swarm:
        return await self.live()

    async def team_list(self) -> list[Team]:
        return await self.teams.list(self.swarm_id)

    async def task_list(self, team: Optional[Team] = None) -> list[Task]:
        return await self.tasks.list(swarm_id=self.swarm_id, team_id=team.id if team else None)

    async def by_status(self, team: Optional[Team] = None) -> dict[str, list[Task]]:
        out: dict[str, list[Task]] = {}
        for t in await self.task_list(team):
            out.setdefault(t.status, []).append(t)
        return out

    async def agent_list(self, team: Optional[Team] = None):
        return await self.agents.list(self.swarm_id, team.id if team else None)

    async def event_list(self, kind: Optional[str] = None, limit: int = 1000):
        evs = list(reversed(await self.events.list(self.swarm_id, limit=limit)))  # oldest first
        return [e for e in evs if kind is None or e.kind == kind]

    async def state(self) -> str:
        return (await self.live()).lifecycle_state

    def calls_of(self, job: str) -> list[dict]:
        return self.fake.calls_for(job)

    # -- driving the engine --------------------------------------------------------------

    async def settle(self, engine: Optional[Engine] = None, timeout: float = 30.0) -> None:
        """Wait until every job the engine launched has finished."""
        e = engine or self.engine
        while e.running:
            await asyncio.wait_for(
                asyncio.gather(*list(e.running.values()), return_exceptions=True), timeout)

    async def step(self, engine: Optional[Engine] = None) -> list:
        """One engine tick, then wait for the jobs it launched."""
        e = engine or self.engine
        actions = await e.tick()
        self.history.append([a.kind.value for a in actions])
        await self.settle(e)
        return actions

    async def _check(self, pred: Predicate) -> bool:
        res = pred()
        if asyncio.iscoroutine(res):
            res = await res
        return bool(res)

    async def run_until(self, pred: Predicate, *, max_steps: int = 300, idle_limit: int = 4,
                        label: str = "the condition", engine: Optional[Engine] = None) -> int:
        """Step the engine until `pred()` holds; fail (with a state dump) after `max_steps`
        steps or after `idle_limit` consecutive steps in which nothing happened."""
        e = engine or self.engine
        idle = 0
        for n in range(1, max_steps + 1):
            actions = await self.step(e)
            if await self._check(pred):
                return n
            idle = idle + 1 if not actions and not e.running else 0
            if idle >= idle_limit:
                raise StuckError(f"engine idle while waiting for {label}\n{await self.dump()}")
        raise StuckError(f"{label} not reached in {max_steps} steps\n{await self.dump()}")

    async def run_to_state(self, state: str, **kw: Any) -> int:
        return await self.run_until(lambda: self._is_state(state), label=f"swarm state {state}",
                                    **kw)

    async def _is_state(self, state: str) -> bool:
        return (await self.state()) == state

    async def dump(self) -> str:
        sw = await self.live()
        lines = [f"swarm {sw.lifecycle_state} dod={'yes' if sw.definition_of_done else 'no'} "
                 f"paused_reason={sw.paused_reason!r}"]
        for t in await self.team_list():
            lines.append(f"team {t.name}: {t.status} std={'yes' if t.definition_of_done else 'no'}"
                         f" gen={t.generation} assessed={t.last_assessed_at is not None}")
        for t in await self.task_list():
            lines.append(f"  task {t.name!r} {t.status} attempts={t.attempts} "
                         f"agent={t.assigned_agent_id and t.assigned_agent_id[:6]}")
        lines.append("last steps: " + " | ".join(",".join(s) or "-" for s in self.history[-8:]))
        for e in (await self.events.list(self.swarm_id, limit=12)):
            lines.append(f"  event {e.kind}: {e.detail[:140]}")
        return "\n".join(lines)

    # -- free-running mode (jobs overlap; for concurrency and cancellation tests) --------------

    async def wait_for(self, pred: Predicate, *, timeout: float = 15.0, interval: float = 0.01,
                       label: str = "the condition") -> None:
        """Poll `pred` (no engine ticks) until it holds; fail after `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while not await self._check(pred):
            if time.monotonic() > deadline:
                raise StuckError(f"timed out after {timeout}s waiting for {label}\n"
                                 f"{await self.dump()}")
            await asyncio.sleep(interval)

    async def run_free(self, pred: Predicate, *, timeout: float = 30.0, interval: float = 0.02,
                       label: str = "the condition", engine: Optional[Engine] = None) -> None:
        """Tick the engine on a short interval WITHOUT waiting for its jobs, like the real
        loop does, until `pred()` holds. Then let the running jobs finish."""
        e = engine or self.engine
        deadline = time.monotonic() + timeout
        while True:
            actions = await e.tick()
            self.history.append([a.kind.value for a in actions])
            if await self._check(pred):
                break
            if time.monotonic() > deadline:
                raise StuckError(f"timed out after {timeout}s waiting for {label}\n"
                                 f"{await self.dump()}")
            await asyncio.sleep(interval)
        await self.settle(e)
