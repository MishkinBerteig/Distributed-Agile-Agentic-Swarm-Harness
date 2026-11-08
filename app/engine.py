"""The DAASH engine: pure `decide()` plus the executor loop (docs/DESIGN-0.1.md section 7).

`decide(snapshot)` is a pure function from database state to a list of Actions.
`Engine` ticks, loads a snapshot, runs deterministic actions inline and launches
LLM jobs (app.jobs) as asyncio tasks with cooldown/backoff on failures.

Run with ``python -m app.engine``.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from app import policy
from app.bus import DecisionBus
from app.config import Settings
from app.db import create_pool
from app.embeddings import build_embedder
from app.jobs import Jobs, is_infrastructure_error
from app.llm import LLMError
from app.models import (Action, ActionKind, AgentRole, Swarm, SwarmSnapshot, SwarmStatus, Task,
                        TaskStatus, TeamStatus)
from app.repo import AgentRepo, EventRepo, SwarmRepo, load_snapshot
from app import services

log = logging.getLogger("daash.engine")

COOLDOWN_BASE_SECONDS = 30.0
COOLDOWN_CAP_SECONDS = 300.0
ESCALATE_AFTER = 3          # consecutive parse/protocol failures before trying a stronger model

#: Actions the engine performs itself, without an LLM.
INLINE_ACTIONS = frozenset({
    ActionKind.PAUSE_FOR_LIMITS, ActionKind.MATERIALIZE_TEAM, ActionKind.COMMIT_TASK,
    ActionKind.FAIL_TEAM, ActionKind.COMPLETE_SWARM, ActionKind.EXPIRE_PROPOSAL})

#: A pending team proposal that has not reached a majority by then is rejected.
PROPOSAL_TTL = timedelta(minutes=30)

#: Human wording for error events.
JOB_LABELS = {
    ActionKind.BOOTSTRAP: "Swarm Coordinator bootstrap",
    ActionKind.GENERATE_STANDARDS: "Quality Judgement standards generation",
    ActionKind.PLAN_TASKS: "Task planning",
    ActionKind.PRIORITIZE: "Mission Judgement prioritisation",
    ActionKind.WORK_TASK: "Delivery work",
    ActionKind.REVIEW_TASK: "Task review",
    ActionKind.ASSESS_MISSION: "Mission assessment",
    ActionKind.RETROSPECTIVE: "Swarm retrospective",
    ActionKind.VOTE: "Team proposal vote",
}


# ---------------------------------------------------------------------------
# decide(): pure
# ---------------------------------------------------------------------------

def decide(snap: SwarmSnapshot) -> list[Action]:
    """What should the engine do next? Pure; never emits an action whose job key is running."""
    swarm = snap.swarm
    running = snap.running_jobs
    out: list[Action] = []

    def emit(action: Action) -> None:
        if action.job_key() not in running:
            out.append(action)

    if swarm.lifecycle_state == SwarmStatus.LEARNING.value:
        if not swarm.retrospective_done:
            emit(Action(kind=ActionKind.RETROSPECTIVE, detail="Swarm retrospective"))
        return out
    if swarm.lifecycle_state != SwarmStatus.ACTIVE.value:
        return out

    reasons = policy.limits_exceeded(swarm, snap.tokens_used, snap.cost_usd, snap.now)
    if reasons:
        emit(Action(kind=ActionKind.PAUSE_FOR_LIMITS, detail=" ".join(reasons)))
        return out

    if swarm.definition_of_done is None or not snap.teams:
        emit(Action(kind=ActionKind.BOOTSTRAP, detail="Vision definition of done and teams"))
        return out

    for prop in snap.proposals:
        if prop.status == "approved" and not prop.created_team_id:
            emit(Action(kind=ActionKind.MATERIALIZE_TEAM, team_id=prop.team_id,
                        task_id=prop.id, proposal_id=prop.id, detail=prop.name))

    active_teams = {t.id for t in snap.teams if t.status == TeamStatus.ACTIVE.value}
    for prop in snap.proposals:
        if prop.status != "pending":
            continue
        if prop.team_id not in active_teams or snap.now - prop.created_at > PROPOSAL_TTL:
            emit(Action(kind=ActionKind.EXPIRE_PROPOSAL, team_id=prop.team_id, task_id=prop.id,
                        proposal_id=prop.id, detail=prop.name))
        elif any(a.team_id == prop.team_id and a.role == AgentRole.MISSION_DELIVERY.value
                 and a.id not in prop.votes for a in snap.agents):
            emit(Action(kind=ActionKind.VOTE, team_id=prop.team_id, task_id=prop.id,
                        proposal_id=prop.id, detail=prop.name))

    lim = swarm.config.limits
    for team in snap.teams:
        if team.status != TeamStatus.ACTIVE.value:
            continue
        if team.definition_of_done is None:
            emit(Action(kind=ActionKind.GENERATE_STANDARDS, team_id=team.id))
            continue

        tasks = [t for t in snap.tasks if t.team_id == team.id]
        agents = [a for a in snap.agents if a.team_id == team.id]
        by_id = {t.id: t for t in snap.tasks}

        for t in tasks:
            if t.status == TaskStatus.REVIEW.value:
                emit(Action(kind=ActionKind.REVIEW_TASK, team_id=team.id, task_id=t.id))
        for t in tasks:
            if t.status == TaskStatus.IN_PROGRESS.value:
                emit(Action(kind=ActionKind.WORK_TASK, team_id=team.id, task_id=t.id,
                            agent_id=t.assigned_agent_id))

        # One commit per idle delivery agent per tick, never the same task twice.
        committed: set[str] = set()
        for agent in policy.idle_delivery_agents(agents, tasks):
            pool = [t for t in tasks if t.id not in committed]
            cand = policy.next_ready_task(pool)
            if cand is None:
                break
            if policy.dor_check(cand, pool, agents, snap.p90_cycle_seconds, snap.now).ok:
                committed.add(cand.id)
                emit(Action(kind=ActionKind.COMMIT_TASK, team_id=team.id, task_id=cand.id,
                            agent_id=agent.id))

        # Mission Judgement approves the next task when nothing startable is Ready.
        has_startable_ready = any(t.status == TaskStatus.READY.value
                                  and policy.deps_done(t, by_id) for t in tasks)
        has_candidate = any(t.status == TaskStatus.PENDING.value
                            and policy.deps_done(t, by_id) for t in tasks)
        if not has_startable_ready and has_candidate:
            emit(Action(kind=ActionKind.PRIORITIZE, team_id=team.id))

        if not policy.live_open_tasks(tasks):
            terminal = [t for t in tasks if t.status in (TaskStatus.DONE.value,
                                                         TaskStatus.FAILED.value)]
            latest = max((t.updated_at for t in terminal), default=None)
            needs_assessment = latest is not None and (
                team.last_assessed_at is None or team.last_assessed_at < latest)
            if needs_assessment:
                emit(Action(kind=ActionKind.ASSESS_MISSION, team_id=team.id))
            elif len(tasks) >= lim.max_tasks_per_team:
                emit(Action(kind=ActionKind.FAIL_TEAM, team_id=team.id,
                            detail=f"{len(tasks)} of {lim.max_tasks_per_team} tasks used"))
            else:
                emit(Action(kind=ActionKind.PLAN_TASKS, team_id=team.id))

    if snap.teams and all(t.status in (TeamStatus.ACCOMPLISHED.value, TeamStatus.FAILED.value)
                          for t in snap.teams):
        emit(Action(kind=ActionKind.COMPLETE_SWARM))
    return out


# ---------------------------------------------------------------------------
# Cooldowns (pure given an injectable clock)
# ---------------------------------------------------------------------------

def backoff_seconds(failures: int, base: float = COOLDOWN_BASE_SECONDS,
                    cap: float = COOLDOWN_CAP_SECONDS) -> float:
    """30s for the first failure, doubling per consecutive failure, capped at 5 minutes."""
    if failures <= 0:
        return 0.0
    return min(cap, base * (2 ** (failures - 1)))


class Cooldowns:
    """Per-job-key failure counters with exponential backoff; reset on success."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._until: dict[str, float] = {}

    def fail(self, key: str) -> tuple[float, int]:
        """Record a failure; returns (cooldown seconds, consecutive failures)."""
        n = self._failures.get(key, 0) + 1
        self._failures[key] = n
        delay = backoff_seconds(n)
        self._until[key] = self._clock() + delay
        return delay, n

    def success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._until.pop(key, None)

    def failures(self, key: str) -> int:
        return self._failures.get(key, 0)

    def active_keys(self) -> set[str]:
        now = self._clock()
        return {k for k, until in self._until.items() if until > now}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class Engine:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()
        self.pool = None
        self.bus: Optional[DecisionBus] = None
        self.jobs: Optional[Jobs] = None
        self.running: dict[str, asyncio.Task] = {}
        self.cooldowns = Cooldowns()
        self.escalation: dict[str, int] = {}
        self._sem = asyncio.Semaphore(1)
        self._sem_limit = 0

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        s = self.settings
        for attempt in range(1, 31):
            try:
                self.pool = await create_pool(s.DATABASE_URL)
                break
            except Exception as exc:  # noqa: BLE001 - db may still be starting
                log.warning("database not ready (%s); retry %d", exc, attempt)
                await asyncio.sleep(2)
        if self.pool is None:
            raise RuntimeError("could not connect to the database")
        self.bus = DecisionBus(s.REDIS_URL, s.BUS_TTL_SECONDS, s.BUS_SIGNALS_MAX)
        llm = services.build_llm(s)
        embedder = build_embedder(s.EMBEDDING_MODEL, s.EMBEDDING_USE_MODEL)
        self.jobs = Jobs(self.pool, llm, self.bus, embedder, s)
        reset = await AgentRepo(self.pool).reset_busy()
        log.info("engine started (tick %.1fs, llm %s, %d busy agents reset)",
                 s.ENGINE_TICK_SECONDS, s.LLM_ENDPOINT, reset)

    async def shutdown(self) -> None:
        tasks = list(self.running.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.pool is not None:
            try:
                await AgentRepo(self.pool).reset_busy()
            except Exception:  # noqa: BLE001
                pass
        if self.bus is not None:
            try:
                await self.bus.close()
            except Exception:  # noqa: BLE001
                pass
        if self.pool is not None:
            await self.pool.close()
        log.info("engine stopped")

    async def run(self, stop: asyncio.Event) -> None:
        if not self.settings.ENGINE_ENABLED:
            log.info("engine disabled (DAASH_ENGINE_ENABLED=false); idling")
            await stop.wait()
            return
        await self.start()
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001 - a tick must never kill the loop
                    log.exception("tick failed")
                try:
                    await asyncio.wait_for(stop.wait(), self.settings.ENGINE_TICK_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.shutdown()

    # -- one tick ----------------------------------------------------------------

    def _sync_semaphore(self, swarm: Swarm) -> None:
        limit = swarm.config.limits.max_simultaneous_agents
        if limit != self._sem_limit:
            self._sem = asyncio.Semaphore(limit)
            self._sem_limit = limit

    async def tick(self) -> list[Action]:
        swarm = await SwarmRepo(self.pool).get_live()
        if swarm is None or swarm.lifecycle_state not in (SwarmStatus.ACTIVE.value,
                                                          SwarmStatus.LEARNING.value):
            return []
        self._sync_semaphore(swarm)
        running = set(self.running) | self.cooldowns.active_keys()
        snap = await load_snapshot(self.pool, swarm.id, datetime.now(timezone.utc), running)
        if snap is None:
            return []
        actions = decide(snap)
        for action in actions:
            if action.kind in INLINE_ACTIONS:
                await self._run_inline(snap.swarm, action)
            else:
                self._launch(snap.swarm, action)
        return actions

    async def _run_inline(self, swarm: Swarm, action: Action) -> None:
        try:
            await self.jobs.run(swarm, action)
            log.info("done %s", action.job_key())
        except Exception:  # noqa: BLE001
            log.exception("inline action %s failed", action.job_key())

    def _launch(self, swarm: Swarm, action: Action) -> None:
        key = action.job_key()
        if key in self.running:
            return
        log.info("launch %s", key)
        self.running[key] = asyncio.create_task(self._run_llm_job(swarm, action, key), name=key)

    async def _run_llm_job(self, swarm: Swarm, action: Action, key: str) -> None:
        sem = self._sem
        try:
            async with sem:
                await self.jobs.run(swarm, action, self.escalation.get(key, 0))
            self.cooldowns.success(key)
            self.escalation.pop(key, None)
            log.info("finished %s", key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._on_failure(swarm, action, key, exc)
        finally:
            self.running.pop(key, None)

    async def _on_failure(self, swarm: Swarm, action: Action, key: str, exc: Exception) -> None:
        delay, n = self.cooldowns.fail(key)
        label = JOB_LABELS.get(action.kind, action.kind.value)
        if isinstance(exc, LLMError):
            reason = ("the language model gateway is unreachable or overloaded"
                      if is_infrastructure_error(exc) else "the model gave an unusable answer")
            detail = str(exc)[:200]
            log.warning("%s failed (%s: %s); cooldown %.0fs (failure %d)", key, exc.code,
                        detail, delay, n)
        else:
            reason = "an unexpected internal error occurred"
            detail = f"{type(exc).__name__}: {exc}"[:200]
            log.exception("%s crashed; cooldown %.0fs (failure %d)", key, delay, n)
        events = EventRepo(self.pool)
        try:
            if n == 1:
                await events.add(
                    swarm.id, "error", team_id=action.team_id, task_id=action.task_id,
                    actor="engine",
                    detail=f"{label} failed because {reason} ({detail}). It will be retried "
                           f"automatically in {delay:.0f} seconds, with longer waits if it "
                           f"keeps failing.")
            if (isinstance(exc, LLMError) and exc.code in ("parse", "protocol")
                    and n % ESCALATE_AFTER == 0):
                self.escalation[key] = n // ESCALATE_AFTER
                await events.add(
                    swarm.id, "error", team_id=action.team_id, task_id=action.task_id,
                    actor="engine",
                    detail=f"{label} failed {n} times in a row; the next try uses a more "
                           f"capable model.")
        except Exception:  # noqa: BLE001
            log.exception("could not write error event")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    async def runner() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover - non-unix
                pass
        await Engine().run(stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
