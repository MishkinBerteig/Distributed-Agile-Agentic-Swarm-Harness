"""Engine job implementations (docs/DESIGN-0.1.md section 7.2).

`Jobs` holds one method per `ActionKind`. The engine (app.engine) decides what to
run and handles scheduling, cooldowns and error policy; everything here just does
the work and raises on failure (LLMError for gateway problems). Pure helpers are
module-level functions so they can be unit-tested without a database.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import asyncpg

import types

from app import policy, prompts, remote, services
from app.bus import DecisionBus
from app.checks import is_deterministic, run_check, validate_check_spec
from app.config import Settings
from app.llm import LLMError, Usage
from app.models import (Action, ActionKind, Agent, AgentRole, AgentState, CheckKind,
                        CreatorKind, CriterionCreate, CriterionStatus, NATIVE_HARNESS, Swarm,
                        SwarmStatus, Task, TaskCreate,
                        TaskStatus, Team, TeamStatus)
from app.repo import (AgentRepo, CallRepo, EventRepo, LearningRepo, MemoryRepo, ProposalRepo,
                      RemoteJobRepo, RunnerRepo, SwarmRepo, TaskRepo, TeamRepo, VerdictRepo)
from app.routing import choose_model, ladder, routing_stats
from app.tools import Tool, ToolError, ToolRegistry, safe_path, workspace_tools

log = logging.getLogger("daash.jobs")

FILE_CLIP = 12_000          # characters of each deliverable file shown to the judges
LISTING_LIMIT = 80          # workspace files listed in prompts
BUS_TIMEOUT = 2.0           # seconds; the bus is best-effort
MAX_LEASE_EXPIRIES = 3      # a remote job whose runner vanished this often is given up on

DELIVERY = AgentRole.MISSION_DELIVERY.value
MJ = AgentRole.MISSION_JUDGEMENT.value
QJ = AgentRole.QUALITY_JUDGEMENT.value
COORD = AgentRole.SWARM_COORDINATOR.value


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class SwarmNotActive(Exception):
    """The swarm left the state a job needs (ACTIVE, or LEARNING for the retrospective) while the
    job ran. Its results were discarded; this is not a failure and is not retried."""


class Deferred(Exception):
    """Mission Judgement approved nothing on purpose: the team's candidates cannot start yet
    (for example they need another team's work). Not a failure. The engine holds this job until
    more work is Done in the swarm (`done_count` changes) or a few minutes pass."""

    def __init__(self, done_count: int, detail: str = "") -> None:
        super().__init__(detail)
        self.done_count = done_count


class RemoteError(LLMError):
    """A remote attempt that says nothing about the work: the runner reported an infrastructure
    `error`, or kept vanishing. An LLMError with code "remote" so the engine treats it like gateway
    trouble: cooldown and retry, no attempt counted (docs/DESIGN-0.3.md section 6)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="remote")


class RefCounts:
    """Running-job reference counts per key (agent id): BUSY while count > 0."""

    def __init__(self) -> None:
        self._n: dict[str, int] = {}

    def enter(self, key: str) -> bool:
        """Count one more job; True if this is the first (the agent just became busy)."""
        n = self._n.get(key, 0)
        self._n[key] = n + 1
        return n == 0

    def exit(self, key: str) -> bool:
        """Count one job less; True if none remain (the agent is idle again)."""
        n = self._n.get(key, 0) - 1
        if n <= 0:
            self._n.pop(key, None)
            return True
        self._n[key] = n
        return False

    def count(self, key: str) -> int:
        return self._n.get(key, 0)


def prioritize_slots(n_delivery: int, ready_startable: int) -> int:
    """How many tasks Mission Judgement may approve: free buffer slots, at least 1."""
    return max(1, max(1, n_delivery) - ready_startable)


def is_infrastructure_error(exc: BaseException) -> bool:
    """Gateway trouble that says nothing about the work: connection, timeout, 5xx, 429."""
    if not isinstance(exc, LLMError):
        return False
    if exc.code in ("connection", "timeout", "remote"):
        return True
    return exc.code == "http" and exc.status is not None and (exc.status >= 500 or exc.status == 429)


def map_depends(depends_idx: Iterable[int], created_ids: list[str]) -> list[str]:
    """Map planned-task dependency positions to the ids of already created tasks.

    Out-of-range or not-yet-created positions are dropped (parse_plan already
    restricts them to earlier tasks)."""
    out: list[str] = []
    for i in depends_idx:
        if 0 <= i < len(created_ids) and created_ids[i] not in out:
            out.append(created_ids[i])
    return out


def dedupe_files(files: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication of workspace paths (ignores empty names)."""
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        f = (f or "").strip()
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def outcomes_from_tasks(tasks: Iterable[Task]) -> list[tuple[str, Optional[str], bool]]:
    """(model, complexity, success) for Done/Failed tasks that recorded a model."""
    out = []
    for t in tasks:
        if t.status not in (TaskStatus.DONE.value, TaskStatus.FAILED.value):
            continue
        model = (t.deliverable or {}).get("model")
        if model:
            out.append((str(model), t.complexity, t.status == TaskStatus.DONE.value))
    return out


def stats_from_tasks(tasks: Iterable[Task]) -> dict:
    return routing_stats(outcomes_from_tasks(tasks))


def escalate_model(base: str, config, steps: int) -> str:
    """`steps` rungs above `base` on the allowed ladder (capped at the top)."""
    if steps <= 0:
        return base
    lad = ladder(config)
    if not lad:
        return base
    idx = lad.index(base) if base in lad else 0
    return lad[min(idx + steps, len(lad) - 1)]


def delivery_spec_model(cfg, agent_name: str) -> str:
    """The `model` of the agent's delivery_harnesses entry ("" = use routing), found from its
    name 'Delivery-<i>' (cycled like services.delivery_spec)."""
    if not cfg.delivery_harnesses:
        return ""
    try:
        i = int(agent_name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return ""
    return cfg.delivery_harnesses[(i - 1) % len(cfg.delivery_harnesses)].model or ""


def review_paths(task: Task) -> list[str]:
    """Workspace paths the judges should see: deliverable files, artifact, check paths."""
    paths: list[str] = list((task.deliverable or {}).get("files") or [])
    if task.artifact:
        paths.append(task.artifact)
    paths.extend(c.check_path for c in task.criteria if c.check_path)
    return dedupe_files(paths)


ASSESS_MAX_FILES = 12
ASSESS_TOTAL_CHARS = 40_000


def assess_clip(n_files: int) -> int:
    """Per-file excerpt length for the mission assessment: up to 4000 characters each, shrinking
    (not below 1500) so up to ASSESS_MAX_FILES excerpts stay within ASSESS_TOTAL_CHARS."""
    n = max(1, min(n_files, ASSESS_MAX_FILES))
    return max(1500, min(4000, ASSESS_TOTAL_CHARS // n))


def read_deliverable_files(root: Path, paths: Iterable[str],
                           limit: int = FILE_CLIP) -> dict[str, str]:
    """Contents of existing workspace files, each truncated to `limit` characters."""
    out: dict[str, str] = {}
    for rel in paths:
        try:
            p = safe_path(root, rel)
            if not p.is_file():
                continue
            data = p.read_bytes()
        except (ToolError, OSError):
            continue
        if b"\x00" in data[:8192]:   # binary (e.g. a compiled .pyc): PostgreSQL text rejects NUL
            out[rel] = f"[binary file, {len(data)} bytes; not shown]"
            continue
        text = data.decode("utf-8", errors="replace").replace("\x00", "")
        if len(text) > limit:
            text = text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"
        out[rel] = text
    return out


def workspace_listing(root: Path, limit: int = LISTING_LIMIT) -> str:
    """`path (size bytes)` lines for the swarm workspace."""
    if not root.is_dir():
        return ""
    lines = []
    files = sorted(p for p in root.rglob("*")
                   if p.is_file() and p.relative_to(root).parts[0] != ".daash")
    for p in files[:limit]:
        lines.append(f"{p.relative_to(root).as_posix()} ({p.stat().st_size} bytes)")
    if len(files) > limit:
        lines.append(f"... and {len(files) - limit} more files")
    return "\n".join(lines)


def combine_feedback(dod_reasons: list[str], task: Task, qr, mr, failed_checks: list[str],
                     rubric_threshold: float = 0.999, mission_threshold: float = 0.99) -> str:
    """One feedback text for the next attempt from everything the judges said.

    Judges' reasons are included for every criterion below the Definition of Done
    threshold, not just the ones marked failed: otherwise a 0.95 score gives the
    delivery agent nothing to act on."""
    parts: list[str] = []
    parts.extend(f"- {r}" for r in dod_reasons)
    parts.extend(f"- Check failed: {c}" for c in failed_checks)
    short = False
    if qr is not None:
        for ordinal, (passed, score, reason) in sorted(qr.criteria.items()):
            if (not passed or score < rubric_threshold) and reason:
                parts.append(f"- Criterion {ordinal} (score {score:.2f}): {reason}")
                short = True
        if qr.feedback:
            parts.append(f"Quality Judgement: {qr.feedback}")
    if mr is not None:
        if mr.feedback:
            parts.append(f"Mission Judgement: {mr.feedback}")
        elif mr.reasoning and (not mr.expected_value_met or mr.confidence < mission_threshold):
            parts.append(f"Mission Judgement: {mr.reasoning}")
        short = short or mr.confidence < mission_threshold
    if short:
        parts.append("The judges need near-certainty: make every criterion's fulfilment explicit "
                     "and verifiable in the file text itself, and remove anything doubtful.")
    return "\n".join(parts) or "The work did not meet the Definition of Done."


def retrospective_summary(swarm: Swarm, teams: list[Team], tasks: list[Task],
                          feedback_by_task: dict[str, str], p90: Optional[float],
                          tokens: int) -> str:
    """Compact text history of the swarm for the Coordinator's retrospective."""
    lines = [f"Vision: {swarm.vision_statement}",
             f"Vision Definition of Done: {swarm.definition_of_done or '(none)'}",
             f"Tokens used: {tokens}; p90 cycle time: "
             + (f"{p90:.0f}s" if p90 is not None else "n/a")]
    for team in teams:
        lines.append(f"\nTeam '{team.name}' ({team.status}, generation {team.generation}): "
                     f"{team.mission_statement}")
        tt = [t for t in tasks if t.team_id == team.id]
        counts: dict[str, int] = {}
        for t in tt:
            counts[t.status] = counts.get(t.status, 0) + 1
        lines.append("  Tasks: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
                                    or "none"))
        for t in tt:
            if t.status in (TaskStatus.DONE.value, TaskStatus.FAILED.value):
                model = (t.deliverable or {}).get("model", "?")
                lines.append(f"  - [{t.status}] {t.name} (attempts {t.attempts}, "
                             f"{t.complexity or 'unclassified'}, model {model})")
                fb = feedback_by_task.get(t.id)
                if fb:
                    lines.append(f"      judge feedback: {fb[:300]}")
    return "\n".join(lines)


def _clip(s: str, n: int = 300) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------

class Jobs:
    def __init__(self, pool: asyncpg.Pool, llm: Any, bus: Optional[DecisionBus],
                 embedder: Any, settings: Settings) -> None:
        self.pool = pool
        self.llm = llm
        self.bus = bus
        self.embedder = embedder
        self.settings = settings
        self.swarms = SwarmRepo(pool)
        self.teams = TeamRepo(pool)
        self.agents = AgentRepo(pool)
        self.tasks = TaskRepo(pool)
        self.events = EventRepo(pool)
        self.learnings = LearningRepo(pool)
        self.verdicts = VerdictRepo(pool)
        self.proposals = ProposalRepo(pool)
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs = RefCounts()
        # judge replies already paid for in a review that later failed (infra error)
        self._partial: dict[tuple, Any] = {}
        # seconds between checks of a remote job's state (tests shrink it)
        self.remote_poll_seconds = 2.0

    # -- plumbing -------------------------------------------------------------

    def lock(self, agent_id: str) -> asyncio.Lock:
        return self._locks.setdefault(agent_id, asyncio.Lock())

    @asynccontextmanager
    async def busy(self, agent: Agent, task_id: Optional[str] = None):
        """BUSY while at least one job of the agent runs, IDLE after the last one.

        Only Delivery agents are serialized (they own a task and its workspace edits); judges
        and the coordinator may run concurrent jobs."""
        serial = agent.role == DELIVERY
        if serial:
            await self.lock(agent.id).acquire()
        try:
            first = self._refs.enter(agent.id)
            try:
                if first or serial:
                    await self.agents.set_state(agent.id, AgentState.BUSY.value, task_id)
                yield
            finally:
                # Always decrement, also on errors and cancellation.
                if self._refs.exit(agent.id):
                    try:
                        await self.agents.set_state(agent.id, AgentState.IDLE.value, None)
                    except BaseException as exc:  # noqa: BLE001 - reset_busy() cleans up on restart
                        log.warning("could not set agent %s idle: %r", agent.name, exc)
        finally:
            if serial:
                self.lock(agent.id).release()

    async def _require_state(self, swarm: Swarm, allowed: tuple[str, ...] = ("ACTIVE",)) -> None:
        """Raise SwarmNotActive unless the swarm is (still) in one of `allowed` states."""
        cur = await self.swarms.get(swarm.id)
        if cur is None or cur.lifecycle_state not in allowed:
            raise SwarmNotActive(swarm.id)

    async def signal(self, kind: str, team_id: Optional[str] = None,
                     task_id: Optional[str] = None, **payload: Any) -> None:
        """Best-effort decision-bus signal; never raises."""
        if self.bus is None:
            return
        try:
            await asyncio.wait_for(self.bus.publish(kind, team_id, task_id, payload), BUS_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            log.debug("bus signal %s dropped: %r", kind, exc)

    def _embed(self, text: str) -> Optional[list[float]]:
        if self.embedder is None or not text:
            return None
        try:
            return self.embedder.embed(text)
        except Exception:  # noqa: BLE001
            return None

    def workspace(self, swarm: Swarm) -> Path:
        return services.ensure_workspace(self.settings, swarm.id)

    async def _json_job(self, swarm: Swarm, agent: Agent, team: Optional[Team], purpose: str,
                        model: str, user: str, parse, task_id: Optional[str] = None,
                        query: str = "", allowed: tuple[str, ...] = ("ACTIVE",)):
        """One complete_json call with prompt context, accounting and busy state."""
        async with self.busy(agent, task_id):
            system = await services.agent_system_prompt(
                self.pool, swarm, agent, team, query=query, embedder=self.embedder)
            t0 = time.monotonic()
            try:
                parsed, loop = await self.llm.complete_json(model, system, user, parse)
            except LLMError as exc:
                await services.record_call(
                    self.pool, agent, purpose, model, usage=exc.usage, transcript=exc.transcript,
                    ok=False, error=str(exc), task_id=task_id,
                    duration_ms=int((time.monotonic() - t0) * 1000))
                raise
            await services.record_call(
                self.pool, agent, purpose, model, usage=loop.usage, transcript=loop.transcript,
                task_id=task_id, duration_ms=int((time.monotonic() - t0) * 1000))
            # Discard the result (usage is already recorded) if the swarm left the state.
            await self._require_state(swarm, allowed)
            return parsed

    async def _still(self, task_id: str, *statuses: str) -> Optional[Task]:
        """The task as it is now if it still exists in one of `statuses` (the user may have
        rejected, reopened or deleted it while a model call ran), else None."""
        cur = await self.tasks.get(task_id)
        return cur if cur is not None and cur.status in statuses else None

    async def _team_agent(self, swarm: Swarm, team: Team, role: str) -> Agent:
        return await services.team_agent(self.pool, swarm, team, role)

    async def _delivery_agents(self, swarm: Swarm, team: Team) -> list[Agent]:
        return [a for a in await self.agents.list(swarm.id, team.id) if a.role == DELIVERY]

    async def run(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        """Run any action. Raises on failure; the engine owns the error policy."""
        handler = {
            ActionKind.PAUSE_FOR_LIMITS: self.pause_for_limits,
            ActionKind.BOOTSTRAP: self.bootstrap,
            ActionKind.MATERIALIZE_TEAM: self.materialize_team,
            ActionKind.GENERATE_STANDARDS: self.generate_standards,
            ActionKind.PLAN_TASKS: self.plan_tasks,
            ActionKind.PRIORITIZE: self.prioritize,
            ActionKind.COMMIT_TASK: self.commit_task,
            ActionKind.WORK_TASK: self.work_task,
            ActionKind.REVIEW_TASK: self.review_task,
            ActionKind.ASSESS_MISSION: self.assess_mission,
            ActionKind.FAIL_TEAM: self.fail_team,
            ActionKind.COMPLETE_SWARM: self.complete_swarm,
            ActionKind.RETROSPECTIVE: self.retrospective,
            ActionKind.VOTE: self.vote,
            ActionKind.EXPIRE_PROPOSAL: self.expire_proposal,
            ActionKind.REVIEW_VISION: self.review_vision,
        }[action.kind]
        await handler(swarm, action, escalation)

    # -- deterministic jobs -----------------------------------------------------

    async def pause_for_limits(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        fresh = await self.swarms.get(swarm.id)
        if fresh is None or fresh.lifecycle_state != "ACTIVE":
            return
        reason = action.detail or "A resource limit was reached."
        await self.swarms.update(swarm.id, lifecycle_state=policy.transition(
            fresh.lifecycle_state, "pause"), paused_reason=reason)
        await self.events.add(swarm.id, "swarm", f"Swarm paused automatically: {reason}",
                              actor="engine")
        await self.signal("swarm.paused", reason=reason)

    async def complete_swarm(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        await self._complete(swarm)

    async def _complete(self, swarm: Swarm, note: str = "") -> bool:
        """All teams terminal -> VERIFICATION (shared by COMPLETE_SWARM and the vision review)."""
        fresh = await self.swarms.get(swarm.id)
        if fresh is None or fresh.lifecycle_state != "ACTIVE":
            return False
        teams = await self.teams.list(swarm.id)
        if not teams or any(t.status == TeamStatus.ACTIVE.value for t in teams):
            return False
        await self.swarms.update(swarm.id, lifecycle_state=policy.transition(
            fresh.lifecycle_state, "verify"))
        await services.close_open_tasks(self.pool, swarm.id, services.VERIFY_CLOSE_REASON)
        done = sum(1 for t in teams if t.status == TeamStatus.ACCOMPLISHED.value)
        await self.events.add(
            swarm.id, "swarm",
            f"All teams have finished ({done} accomplished, {len(teams) - done} failed). "
            f"The swarm moved to verification: review the artifacts and archive or continue."
            + note,
            actor="engine")
        await self.signal("swarm.verification", teams=len(teams), accomplished=done)
        return True

    async def review_vision(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        """Swarm Coordinator judges the combined result against the vision-level DoD
        (docs/DESIGN-0.3.md section 10): confirmed -> verification; gaps -> reopen teams."""
        fresh = await self.swarms.get(swarm.id)
        if fresh is None or fresh.lifecycle_state != "ACTIVE":
            return
        teams = await self.teams.list(swarm.id)
        if not teams or any(t.status == TeamStatus.ACTIVE.value for t in teams):
            return
        coord = await services.ensure_coordinator(self.pool, swarm)
        tasks = await self.tasks.list(swarm_id=swarm.id)
        done_by_team: dict[str, list[Task]] = {
            t.name: [x for x in tasks if x.team_id == t.id and x.status == TaskStatus.DONE.value]
            for t in teams}
        ws = self.workspace(swarm)
        paths = dedupe_files(p for ts in done_by_team.values() for x in ts
                             for p in review_paths(x))
        files = read_deliverable_files(ws, paths[:12], limit=4000)
        names = [t.name for t in teams]
        threshold = swarm.config.dod_thresholds.mission_confidence
        model = escalate_model(choose_model(COORD, swarm.config), swarm.config, escalation)
        res = await self._json_job(
            swarm, coord, None, "review_vision", model,
            prompts.vision_review_prompt(
                swarm.vision_statement, fresh.definition_of_done or "", teams, done_by_team,
                workspace_listing(ws), files),
            lambda o: prompts.parse_vision_review(o, names, threshold),
            query=swarm.vision_statement)
        # _json_job re-checked ACTIVE; re-read the counter in case of races.
        cur = await self.swarms.get(swarm.id)
        used = (cur.vision_reviews if cur is not None else fresh.vision_reviews) + 1
        await self.swarms.update(swarm.id, vision_reviews=used)

        if res.verdict == "met" and res.confidence >= threshold:
            await self.events.add(
                swarm.id, "swarm", actor=coord.id,
                detail=f"Swarm Coordinator confirmed the vision-level Definition of Done "
                       f"(confidence {res.confidence:.2f}): {_clip(res.summary, 400)}")
            await self._complete(swarm)
            return

        by_name = {t.name: t for t in teams}
        reopened: list[str] = []
        unactionable: list[str] = []
        for name, focus in res.gaps:
            team = by_name[name]
            if team.status == TeamStatus.ACTIVE.value:
                continue    # already reopened by an earlier gap in this answer
            tasks_t = [x for x in tasks if x.team_id == team.id]
            limit = swarm.config.limits.max_tasks_per_team
            if (team.status == TeamStatus.FAILED.value
                    and policy.task_budget_left(tasks_t, limit) <= 0):
                unactionable.append(f"'{name}' (no task budget left): {_clip(focus, 200)}")
                continue
            await self.teams.update(team.id, status=TeamStatus.ACTIVE.value, next_focus=focus)
            await self.events.add(
                swarm.id, "team", team_id=team.id, actor=coord.id,
                detail=f"Swarm Coordinator reopened team '{name}': {_clip(focus, 300)}")
            await self.signal("team.reopened", team_id=team.id)
            reopened.append(name)
        if reopened:
            if unactionable:
                await self.events.add(
                    swarm.id, "swarm", actor=coord.id,
                    detail="Vision review gaps that could not be acted on: "
                           + "; ".join(unactionable))
            return
        listing = "; ".join(f"'{n}': {_clip(f, 200)}" for n, f in res.gaps)
        await self._complete(
            swarm, f" The vision review found open gaps no team could act on: {listing}. "
                   f"{_clip(res.summary, 300)}")

    async def fail_team(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        team = await self.teams.get(action.team_id) if action.team_id else None
        if team is None or team.status != TeamStatus.ACTIVE.value:
            return
        tasks = await self.tasks.list(team_id=team.id)
        limit = swarm.config.limits.max_tasks_per_team
        if policy.task_budget_left(tasks, limit) > 0 or policy.live_open_tasks(tasks):
            return
        await self.teams.update(team.id, status=TeamStatus.FAILED.value)
        done = sum(1 for t in tasks if t.status == TaskStatus.DONE.value)
        await self.events.add(
            swarm.id, "team", team_id=team.id,
            detail=f"Team '{team.name}' failed: it used its whole budget of {limit} tasks "
                   f"({done} Done) without Mission Judgement confirming the mission.")
        await self.learnings.add(
            f"Team '{team.name}' exhausted {limit} tasks without accomplishing its mission "
            f"({team.mission_statement}). Plan fewer, more decisive tasks.",
            "pattern", swarm_id=swarm.id, team_id=team.id,
            embedding=self._embed(team.mission_statement))
        await self.signal("team.failed", team_id=team.id)

    async def materialize_team(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        prop = await self.proposals.get(action.proposal_id) if action.proposal_id else None
        if prop is None or prop.status != "approved" or prop.created_team_id:
            return
        teams = await self.teams.list(swarm.id)
        limit = swarm.config.limits.max_teams
        if len(teams) >= limit:
            await self.proposals.set_status(prop.id, "rejected")
            await self.events.add(
                swarm.id, "proposal", team_id=prop.team_id,
                detail=f"Proposal for new team '{prop.name}' was approved by vote but "
                       f"rejected: the swarm already has the maximum of {limit} teams.")
            return
        # Idempotent: a crash after creating the team but before set_status must not duplicate it.
        team = next((t for t in teams if t.name == prop.name
                     and t.creator_team_id == prop.team_id), None)
        if team is not None:
            await services.ensure_team_agents(self.pool, swarm, team)
        else:
            team = await services.create_team(
                self.pool, swarm, prop.name, prop.mission_statement,
                creator_kind=CreatorKind.TEAM.value, creator_team_id=prop.team_id,
                actor=prop.proposer_agent_id)
        await self.proposals.set_status(prop.id, "approved", created_team_id=team.id)
        await self.signal("team.created", team_id=team.id, name=team.name)

    async def expire_proposal(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        prop = await self.proposals.get(action.proposal_id) if action.proposal_id else None
        if prop is None or prop.status != "pending":
            return
        team = await self.teams.get(prop.team_id)
        why = ("the proposing team is no longer active"
               if team is None or team.status != TeamStatus.ACTIVE.value
               else "no majority within 30 minutes")
        await self.proposals.set_status(prop.id, "rejected")
        await self.events.add(
            swarm.id, "proposal", team_id=prop.team_id, actor="engine",
            detail=f"Proposal for new team '{prop.name}' was rejected: {why}.")
        await self.signal("proposal.decided", prop.team_id, outcome="rejected",
                          proposal_id=prop.id)

    async def commit_task(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        """Re-check the Definition of Ready against fresh state, then commit."""
        task = await self.tasks.get(action.task_id) if action.task_id else None
        if task is None or task.status != TaskStatus.READY.value:
            return
        team_tasks = await self.tasks.list(team_id=task.team_id)
        team_agents = await self.agents.list(swarm.id, task.team_id)
        live = await self.swarms.get(swarm.id)
        if live is None or live.lifecycle_state != "ACTIVE":
            return
        now = _now()
        res = policy.dor_check(task, team_tasks, team_agents,
                               await self._p90(swarm.id), now)
        if not res.ok:
            log.info("commit of %r skipped: %s", task.name, "; ".join(res.reasons))
            return
        idle = policy.idle_delivery_agents(team_agents, team_tasks)
        if not idle:
            return
        agent = next((a for a in idle if a.id == action.agent_id), idle[0])
        await self.tasks.update(task.id, status=TaskStatus.IN_PROGRESS.value,
                                assigned_agent_id=agent.id, committed_at=now)
        await self.events.add(
            swarm.id, "committed", team_id=task.team_id, task_id=task.id,
            from_status=TaskStatus.READY.value, to_status=TaskStatus.IN_PROGRESS.value,
            actor="engine",
            detail=f"{agent.name} committed to '{task.name}': the Definition of Ready is met.")
        await self.signal("task.committed", task.team_id, task.id, agent=agent.name)

    async def _p90(self, swarm_id: str) -> Optional[float]:
        row = await self.pool.fetchrow(
            "SELECT count(*) AS n, (percentile_disc(0.9) WITHIN GROUP (ORDER BY secs))"
            "::double precision AS p90 FROM (SELECT extract(epoch FROM done_at - committed_at)"
            "::double precision AS secs FROM tasks WHERE swarm_id = $1 AND status = 'Done' "
            "AND done_at IS NOT NULL AND committed_at IS NOT NULL ORDER BY done_at DESC "
            "LIMIT 100) r", swarm_id)
        return row["p90"] if row and row["n"] >= 5 else None

    # -- coordinator ----------------------------------------------------------

    async def bootstrap(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        coord = await services.ensure_coordinator(self.pool, swarm)
        existing = await self.teams.list(swarm.id)
        need_teams = not existing
        max_teams = swarm.config.limits.max_teams
        model = escalate_model(choose_model(COORD, swarm.config), swarm.config, escalation)
        res = await self._json_job(
            swarm, coord, None, "bootstrap", model,
            prompts.bootstrap_prompt(max_teams, need_teams),
            lambda o: prompts.parse_bootstrap(o, max_teams, need_teams),
            query=swarm.vision_statement)
        fresh = await self.swarms.get(swarm.id)
        if fresh is not None and fresh.definition_of_done is None:
            await self.swarms.update(swarm.id, definition_of_done=res.vision_dod)
            await self.events.add(
                swarm.id, "swarm", actor=coord.id,
                detail="Swarm Coordinator wrote the vision-level Definition of Done: "
                       + _clip(res.vision_dod, 400))
        if need_teams and not await self.teams.list(swarm.id):
            for name, mission in res.teams:
                team = await services.create_team(
                    self.pool, swarm, name, mission,
                    creator_kind=CreatorKind.SWARM_COORDINATOR.value, actor=coord.id)
                await self.signal("team.created", team_id=team.id, name=team.name)

    async def retrospective(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        coord = await services.ensure_coordinator(self.pool, swarm)
        teams = await self.teams.list(swarm.id)
        tasks = await self.tasks.list(swarm_id=swarm.id)
        feedback: dict[str, str] = {}
        for t in tasks:
            if t.status in (TaskStatus.DONE.value, TaskStatus.FAILED.value):
                vs = await self.verdicts.list_for_task(t.id)
                notes = [v.reasoning for v in vs if not v.passed and v.reasoning]
                if notes:
                    feedback[t.id] = notes[-1]
        tokens = await CallRepo(self.pool).tokens_used(swarm.id)
        summary = retrospective_summary(swarm, teams, tasks, feedback,
                                        await self._p90(swarm.id), tokens)
        model = escalate_model(choose_model(COORD, swarm.config), swarm.config, escalation)
        learned = await self._json_job(
            swarm, coord, None, "retrospective", model, prompts.retrospective_prompt(summary),
            prompts.parse_retrospective, query=swarm.vision_statement,
            allowed=("LEARNING",))
        for kind, content in learned:
            await self.learnings.add(content, kind, swarm_id=None, created_by=coord.id,
                                     embedding=self._embed(content))
        await self.swarms.update(swarm.id, retrospective_done=True)
        await self.events.add(
            swarm.id, "swarm", actor=coord.id,
            detail=f"Swarm Coordinator finished the retrospective and recorded "
                   f"{len(learned)} global learnings for future swarms.")

    # -- team standards / planning / prioritising -----------------------------------

    async def generate_standards(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        team = await self.teams.get(action.team_id)
        if team is None or team.definition_of_done is not None:
            return
        eff = swarm
        if escalation:
            eff = swarm.model_copy(deep=True)
            eff.config.role_models.quality_judgement = escalate_model(
                choose_model(QJ, swarm.config), swarm.config, escalation)
        qj = await self._team_agent(swarm, team, QJ)
        async def still_ok() -> bool:
            cur = await self.swarms.get(swarm.id)
            return cur is not None and cur.lifecycle_state == "ACTIVE"

        async with self.busy(qj):
            await services.generate_standards(self.pool, self.llm, eff, team,
                                              embedder=self.embedder, manage_state=False,
                                              still_ok=still_ok)

    async def plan_tasks(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        team = await self.teams.get(action.team_id)
        if team is None:
            return
        lim = swarm.config.limits
        tasks = await self.tasks.list(team_id=team.id)
        cap = min(lim.max_new_tasks_per_plan,
                  policy.task_budget_left(tasks, lim.max_tasks_per_team))
        if cap <= 0 or policy.live_open_tasks(tasks):
            return
        agent = await self._team_agent(swarm, team, DELIVERY)
        done = [t for t in tasks if t.status == TaskStatus.DONE.value]
        model = escalate_model(choose_model(DELIVERY, swarm.config), swarm.config, escalation)
        others = [t for t in await self.teams.list(swarm.id) if t.id != team.id]
        user = prompts.plan_prompt(team.name, done, [], cap, team.next_focus,
                                   workspace_listing(self.workspace(swarm)), others)
        planned = await self._json_job(
            swarm, agent, team, "plan_tasks", model, user,
            lambda o: prompts.parse_plan(o, cap), query=team.mission_statement)
        # Re-check just before writing: another job may have changed the team.
        tasks = await self.tasks.list(team_id=team.id)
        room = policy.task_budget_left(tasks, lim.max_tasks_per_team)
        if room <= 0 or policy.live_open_tasks(tasks):
            return
        created: list[Task] = []
        for pt in planned[:room]:
            data = TaskCreate(team_id=team.id, name=pt.name, description=pt.description,
                              artifact=pt.artifact, depends_on=map_depends(
                                  pt.depends_on, [t.id for t in created]),
                              acceptance_criteria=pt.criteria)
            created.append(await self.tasks.create(
                swarm.id, data, created_by=agent.id,
                embedding=self._embed(f"{pt.name}. {pt.description}")))
        names = ", ".join(f"'{t.name}'" for t in created)
        await self.events.add(
            swarm.id, "created", team_id=team.id, actor=agent.id,
            detail=f"{agent.name} planned {len(created)} new task(s) for team '{team.name}': "
                   f"{names}.")

    async def prioritize(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        team = await self.teams.get(action.team_id)
        if team is None:
            return
        tasks = await self.tasks.list(team_id=team.id)
        by_id = {t.id: t for t in tasks}
        candidates = [t for t in tasks if t.status == TaskStatus.PENDING.value
                      and policy.deps_done(t, by_id)]
        if not candidates:
            return
        ready = [t for t in tasks if t.status == TaskStatus.READY.value]
        cand_ids = [t.id for t in candidates]
        valid = cand_ids + [t.id for t in ready]
        done = [t for t in tasks if t.status == TaskStatus.DONE.value]
        mj = await self._team_agent(swarm, team, MJ)
        model = escalate_model(choose_model(MJ, swarm.config), swarm.config, escalation)
        all_teams = await self.teams.list(swarm.id)
        others = [t for t in all_teams if t.id != team.id]
        ready_startable = sum(1 for t in ready if policy.deps_done(t, by_id))
        slots = prioritize_slots(len(await self._delivery_agents(swarm, team)), ready_startable)
        # Deferring only helps while other work can still finish; otherwise it would wait forever.
        can_wait = policy.work_in_flight(team.id, all_teams,
                                         await self.tasks.list(swarm_id=swarm.id))
        user = prompts.prioritize_prompt(candidates, done, team.definition_of_ready, others,
                                         slots=slots,
                                         workspace_files=workspace_listing(self.workspace(swarm)),
                                         can_wait=can_wait)
        res = await self._json_job(
            swarm, mj, team, "prioritize", model, user,
            lambda o: prompts.parse_prioritize(o, valid, slots, allow_defer=can_wait),
            query=team.mission_statement)

        # The user may have changed or deleted tasks while Mission Judgement thought.
        fresh = {t.id: t.status for t in await self.tasks.list(team_id=team.id)}
        res.rejections = [(tid, r) for tid, r in res.rejections
                          if fresh.get(tid) == by_id[tid].status]
        res.order = [tid for tid in res.order if fresh.get(tid) == by_id[tid].status]
        now = _now()
        rejected = {tid for tid, _ in res.rejections}
        for tid, reason in res.rejections:
            t = by_id[tid]
            await self.tasks.update(tid, status=TaskStatus.REJECTED.value,
                                    rejection_reason=reason)
            await self.events.add(
                swarm.id, "rejected", team_id=team.id, task_id=tid, actor=mj.id,
                from_status=t.status, to_status=TaskStatus.REJECTED.value,
                detail=f"{mj.name} rejected '{t.name}': {reason}")
        rank = 0
        order = [tid for tid in res.order if tid not in rejected]
        approve = [tid for tid in dict.fromkeys(res.next_task_ids or [res.next_task_id])
                   if tid in cand_ids and tid in order][:slots]
        if not approve and not res.deferred:
            first = next((tid for tid in order if tid in cand_ids), None)
            approve = [first] if first else []
        for tid in order:
            rank += 1
            fields: dict[str, Any] = {"priority": rank}
            if tid in approve:
                fields.update(status=TaskStatus.READY.value, approved_at=now)
            await self.tasks.update(tid, **fields)
            if tid in approve:
                await self.events.add(
                    swarm.id, "approved", team_id=team.id, task_id=tid, actor=mj.id,
                    from_status=TaskStatus.PENDING.value, to_status=TaskStatus.READY.value,
                    detail=f"{mj.name} approved '{by_id[tid].name}' as next priority (#{rank}) "
                           f"out of {len(cand_ids)} candidate(s) for team '{team.name}'.")
        waiting = [tid for tid in order if tid in cand_ids and tid not in approve]
        if not approve and waiting:
            names = ", ".join(f"'{by_id[tid].name}'" for tid in waiting[:3])
            more = f" and {len(waiting) - 3} more" if len(waiting) > 3 else ""
            await self.events.add(
                swarm.id, "deferred", team_id=team.id, actor=mj.id,
                detail=f"{mj.name} approved no task for team '{team.name}' yet: {names}{more} "
                       f"cannot start yet and stay Pending"
                       + (f" (waiting for: {res.defer_reason})" if res.defer_reason else "")
                       + ". It will look again when more work is done.")
            swarm_tasks = await self.tasks.list(swarm_id=swarm.id)
            raise Deferred(sum(1 for t in swarm_tasks if t.status == TaskStatus.DONE.value),
                           f"team {team.name}: nothing can start yet")

    # -- work -------------------------------------------------------------------

    async def work_task(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        task = await self.tasks.get(action.task_id)
        if task is None or task.status != TaskStatus.IN_PROGRESS.value:
            return
        team = await self.teams.get(task.team_id)
        if team is None:
            return
        agent = await self.agents.get(task.assigned_agent_id) if task.assigned_agent_id else None
        if agent is None:
            agent = await self._team_agent(swarm, team, DELIVERY)
            await self.tasks.update(task.id, assigned_agent_id=agent.id)
        cfg = swarm.config

        async with self.busy(agent, task.id):
            if task.complexity is None:
                task = await self._classify(swarm, agent, team, task)
            all_tasks = await self.tasks.list(swarm_id=swarm.id)
            stats = stats_from_tasks(all_tasks)
            model = choose_model(DELIVERY, cfg, task.complexity, task.attempts, stats)
            if agent.harness != NATIVE_HARNESS:
                spec_model = delivery_spec_model(cfg, agent.name)
                if spec_model:
                    model = escalate_model(spec_model, cfg, task.attempts)
                if await self._remote_work(swarm, team, agent, task, model):
                    return
                # nobody picked the job up in time: run this attempt on the built-in harness
                model = choose_model(DELIVERY, cfg, task.complexity, task.attempts, stats)
            await self._native_work(swarm, team, agent, task, model)

    async def _native_work(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                           model: str) -> None:
        """One attempt on the built-in "daash" harness (the tool loop)."""
        cfg = swarm.config
        ws = self.workspace(swarm)
        registry = ToolRegistry()
        for tool in workspace_tools(ws, registry):
            registry.add(tool)
        submitted: dict[str, str] = {}
        pending = await self._pending_proposals(swarm, team, agent)
        for tool in self._engine_tools(swarm, team, agent, task, submitted):
            if tool.name == "vote_on_proposal" and not pending:
                continue
            registry.add(tool)

        system = await services.agent_system_prompt(
            self.pool, swarm, agent, team, query=f"{task.name}. {task.description}",
            embedder=self.embedder)
        user = prompts.work_prompt(task, task.criteria, task.feedback,
                                   workspace_listing(ws), pending)
        t0 = time.monotonic()
        try:
            loop = await self.llm.run_tool_loop(model, system, user, registry,
                                                cfg.limits.max_tool_steps)
        except LLMError as exc:
            await services.record_call(
                self.pool, agent, "work_task", model, usage=exc.usage,
                transcript=exc.transcript, ok=False, error=str(exc), task_id=task.id,
                duration_ms=int((time.monotonic() - t0) * 1000))
            await self._require_state(swarm)
            if is_infrastructure_error(exc):
                raise
            await self._attempt_failed(
                swarm, team, agent, task, model, registry,
                f"The model call failed ({exc}).")
            return
        await services.record_call(
            self.pool, agent, "work_task", model, usage=loop.usage, transcript=loop.transcript,
            task_id=task.id, duration_ms=int((time.monotonic() - t0) * 1000))
        await self._require_state(swarm)

        if await self._still(task.id, TaskStatus.IN_PROGRESS.value) is None:
            log.info("work on %r dropped: the task changed while the agent worked", task.name)
            return
        files = dedupe_files(registry.files_written)
        if loop.stopped_by == "terminal" and loop.terminal_tool == "submit":
            summary = submitted.get("summary") or loop.terminal_output or ""
            await self.tasks.update(
                task.id, status=TaskStatus.REVIEW.value,
                deliverable={"summary": summary, "files": files, "model": model})
            await self.events.add(
                swarm.id, "submitted", team_id=team.id, task_id=task.id, actor=agent.id,
                from_status=TaskStatus.IN_PROGRESS.value, to_status=TaskStatus.REVIEW.value,
                detail=f"{agent.name} submitted '{task.name}' for review "
                       f"({len(files)} file(s) written, model {model}): {_clip(summary, 200)}")
            await self.signal("task.submitted", team.id, task.id, files=files)
            return
        why = ("it used all its tool steps" if loop.stopped_by == "max_steps"
               else "it answered in text")
        await self._attempt_failed(
            swarm, team, agent, task, model, registry,
            f"{why} without calling `submit`.", loop.final_text)

    # -- remote work (docs/DESIGN-0.3.md section 6) -----------------------------------------------

    async def _remote_work(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                           model: str) -> bool:
        """One attempt on an external agent CLI through a runner.

        Returns False if no runner leased the job in time (the caller then runs it natively),
        True when the attempt was handled (submitted, failed or dropped). Raises RemoteError for
        infrastructure trouble and SwarmNotActive / CancelledError when the swarm moved on."""
        cfg = swarm.config
        repo = RemoteJobRepo(self.pool)
        ws = self.workspace(swarm)
        system = await services.agent_system_prompt(
            self.pool, swarm, agent, team, query=f"{task.name}. {task.description}",
            embedder=self.embedder)
        prompt = prompts.remote_work_prompt(task, task.criteria, task.feedback,
                                            workspace_listing(ws))
        payload = {
            "system_prompt": system, "prompt": prompt,
            "limits": {"timeout_seconds": cfg.limits.remote_job_timeout_minutes * 60,
                       "max_files": remote.MAX_FILES, "max_file_bytes": remote.MAX_FILE_BYTES,
                       "max_total_bytes": remote.MAX_TOTAL_BYTES},
            "task": {"name": task.name, "artifact": task.artifact}}
        job = await repo.ensure_active(swarm.id, team.id, task.id, agent.id, task.attempts,
                                       agent.harness, model, payload)
        job_id = job.id
        try:
            while True:
                await repo.requeue_expired(_now())
                job = await repo.get(job_id)
                if job is None:
                    raise RemoteError("the remote job disappeared")
                now = _now()
                if job.expiries >= MAX_LEASE_EXPIRIES and job.status in ("queued", "leased"):
                    await repo.abandon(job.id)
                    raise RemoteError(
                        f"the runner for '{task.name}' vanished {job.expiries} times")
                if job.status == "completed":
                    return await self._apply_remote(swarm, team, agent, task, model, job)
                if job.status in ("cancelled", "abandoned"):
                    await self._require_state(swarm)
                    raise RemoteError(f"the remote job was {job.status} by the coordinator")
                if job.status == "queued":
                    wait = cfg.remote_fallback_minutes
                    if wait is not None and now - job.created_at > timedelta(minutes=wait):
                        await repo.abandon(job.id)
                        await self.events.add(
                            swarm.id, "info", team_id=team.id, task_id=task.id, actor=agent.id,
                            detail=f"No runner offering '{agent.harness}' picked up "
                                   f"'{task.name}' within {wait} minutes, so {agent.name} ran "
                                   f"it on the built-in harness instead.")
                        return False
                elif job.status == "leased":
                    limit = timedelta(minutes=cfg.limits.remote_job_timeout_minutes)
                    if job.leased_at is not None and now - job.leased_at > limit:
                        await repo.cancel(job.id)
                        await self._attempt_failed(
                            swarm, team, agent, task, model, types.SimpleNamespace(files_written=[]),
                            "the remote agent ran out of time.", remote=True)
                        return True
                await asyncio.sleep(self.remote_poll_seconds)
        except asyncio.CancelledError:
            # Pause / verification (B.4): tell the runner through its next heartbeat. An engine
            # shutdown or restart (swarm still ACTIVE) leaves the job alone, so the runner keeps
            # working and the next engine re-attaches to the same job instead of redoing it.
            try:
                await asyncio.shield(self._cancel_remote_unless_active(repo, job_id, swarm.id))
            except BaseException as exc:  # noqa: BLE001 - best effort
                log.warning("could not cancel remote job %s: %r", job_id, exc)
            raise

    async def _cancel_remote_unless_active(self, repo, job_id: str, swarm_id: str) -> None:
        fresh = await self.swarms.get(swarm_id)
        if fresh is None or fresh.lifecycle_state != SwarmStatus.ACTIVE.value:
            await repo.cancel(job_id)

    async def _apply_remote(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                            model: str, job) -> bool:
        """Take a completed remote job's result into the workspace and the task."""
        res = job.result or {}
        outcome = res.get("outcome", "error")
        runner = await RunnerRepo(self.pool).get(job.runner_id) if job.runner_id else None
        runner_name = runner.name if runner else "unknown"
        ws = self.workspace(swarm)
        usage = Usage(**{k: int((res.get("usage") or {}).get(k, 0))
                         for k in ("prompt_tokens", "completion_tokens", "total_tokens")})
        await services.record_call(
            self.pool, agent, "work_task", model, usage=usage, task_id=task.id,
            ok=outcome != "error", error=res.get("error") if outcome == "error" else None,
            duration_ms=int(res.get("duration_ms") or 0),
            transcript=[{"role": "remote", "harness": job.harness, "runner": runner_name,
                         "outcome": outcome, "summary": res.get("summary", ""),
                         "content": res.get("transcript_tail", "")}])
        try:
            await self._require_state(swarm)
        except SwarmNotActive:
            await asyncio.to_thread(shutil.rmtree, remote.staging_dir(ws, job.id), True)
            raise
        if await self._still(task.id, TaskStatus.IN_PROGRESS.value) is None:
            await asyncio.to_thread(shutil.rmtree, remote.staging_dir(ws, job.id), True)
            log.info("remote work on %r dropped: the task changed while the agent worked",
                     task.name)
            return True
        written, _gone = await asyncio.to_thread(remote.apply_staged, ws, job.id,
                                                 res.get("deleted") or [])
        if outcome == "error":
            raise RemoteError(res.get("error") or "the runner reported an infrastructure error")
        files = dedupe_files(written)
        who = f"{agent.name} ({job.harness} on runner '{runner_name}')"
        if outcome == "submitted":
            summary = res.get("summary") or ""
            await self.tasks.update(
                task.id, status=TaskStatus.REVIEW.value,
                deliverable={"summary": summary, "files": files, "model": model,
                             "harness": job.harness, "runner": runner_name})
            await self.events.add(
                swarm.id, "submitted", team_id=team.id, task_id=task.id, actor=agent.id,
                from_status=TaskStatus.IN_PROGRESS.value, to_status=TaskStatus.REVIEW.value,
                detail=f"{who} submitted '{task.name}' for review ({len(files)} file(s) "
                       f"written, model {model}): {_clip(summary, 200)}")
            await self.signal("task.submitted", team.id, task.id, files=files)
            return True
        await self._attempt_failed(
            swarm, team, agent, task, model, types.SimpleNamespace(files_written=files),
            f"{who} did not finish: {res.get('error') or 'no final answer'}.",
            res.get("summary") or "", remote=True)
        return True

    async def _attempt_failed(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                              model: str, registry: ToolRegistry, why: str,
                              final_text: str = "", remote: bool = False) -> None:
        if await self._still(task.id, TaskStatus.IN_PROGRESS.value) is None:
            return
        attempts = task.attempts + 1
        verdict = policy.rework_or_fail(attempts, swarm.config.limits.max_attempts_per_task)
        ending = ("Write the files, check each acceptance criterion, then finish with a short "
                  "summary." if remote else
                  "Write the files, check each acceptance criterion, then call the submit tool "
                  "with a short summary.")
        feedback = f"Attempt {attempts} did not finish: {why} {ending}"
        deliverable = {"summary": _clip(final_text, 500), "files": dedupe_files(
            registry.files_written), "model": model}
        if verdict == "failed":
            await self.tasks.update(task.id, status=TaskStatus.FAILED.value, attempts=attempts,
                                    feedback=feedback, deliverable=deliverable)
            await self.events.add(
                swarm.id, "failed", team_id=team.id, task_id=task.id, actor=agent.id,
                from_status=TaskStatus.IN_PROGRESS.value, to_status=TaskStatus.FAILED.value,
                detail=f"'{task.name}' failed after {attempts} attempt(s): {why}")
            await self._learn_from_failure(swarm, team, task, task.feedback or feedback)
            await self.signal("task.failed", team.id, task.id)
        else:
            await self.tasks.update(task.id, attempts=attempts, feedback=feedback,
                                    deliverable=deliverable)
            await self.events.add(
                swarm.id, "rework", team_id=team.id, task_id=task.id, actor=agent.id,
                detail=f"{agent.name} did not finish '{task.name}' (attempt {attempts} of "
                       f"{swarm.config.limits.max_attempts_per_task}): {why} Trying again.")
            await self.signal("task.rework", team.id, task.id)

    async def _classify(self, swarm: Swarm, agent: Agent, team: Team, task: Task) -> Task:
        model = choose_model("classifier", swarm.config)
        system = await services.agent_system_prompt(
            self.pool, swarm, agent, team, query=task.name, embedder=self.embedder)
        t0 = time.monotonic()
        complexity = "moderate"
        try:
            complexity, loop = await self.llm.complete_json(
                model, system, prompts.classify_prompt(task), prompts.parse_classification)
            await services.record_call(
                self.pool, agent, "classify_task", model, usage=loop.usage,
                transcript=loop.transcript, task_id=task.id,
                duration_ms=int((time.monotonic() - t0) * 1000))
        except LLMError as exc:
            await services.record_call(
                self.pool, agent, "classify_task", model, usage=exc.usage,
                transcript=exc.transcript, ok=False, error=str(exc), task_id=task.id,
                duration_ms=int((time.monotonic() - t0) * 1000))
            if is_infrastructure_error(exc):
                raise
            log.warning("classification of %r failed (%s); defaulting to moderate", task.name, exc)
        updated = await self.tasks.update(task.id, complexity=complexity)
        return updated or task

    async def _pending_proposals(self, swarm: Swarm, team: Team, agent: Agent):
        props = await self.proposals.list(swarm.id, status="pending")
        return [p for p in props if p.team_id == team.id and agent.id not in p.votes]

    def _engine_tools(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                      submitted: dict[str, str]) -> list[Tool]:
        def sarg(args: dict, key: str, required: bool = True) -> str:
            v = args.get(key)
            if not isinstance(v, str) or (required and not v.strip()):
                raise ToolError(f"missing or non-string argument {key!r}")
            return v.strip()

        async def create_subtask(args: dict) -> str:
            name = sarg(args, "name")
            tasks = await self.tasks.list(team_id=team.id)
            if policy.task_budget_left(tasks, swarm.config.limits.max_tasks_per_team) <= 0:
                raise ToolError("the team has used its whole task budget; no more tasks")
            artifact = sarg(args, "artifact", required=False)
            crits = [CriterionCreate(text=c.strip()) for c in (args.get("acceptance_criteria") or [])
                     if isinstance(c, str) and c.strip()]
            if artifact:
                if validate_check_spec(CheckKind.FILE_EXISTS.value, artifact, None):
                    raise ToolError(f"invalid artifact path {artifact!r}")
                crits.append(CriterionCreate(text=f"{artifact} exists",
                                             check_kind=CheckKind.FILE_EXISTS, check_path=artifact))
            if not crits:
                raise ToolError("give at least one acceptance criterion (or an artifact path)")
            sub = await self.tasks.create(
                swarm.id, TaskCreate(team_id=team.id, name=name,
                                     description=sarg(args, "description", required=False),
                                     artifact=artifact, parent_id=task.id,
                                     acceptance_criteria=crits),
                created_by=agent.id, embedding=self._embed(name))
            await self.events.add(
                swarm.id, "created", team_id=team.id, task_id=sub.id, actor=agent.id,
                detail=f"{agent.name} created subtask '{name}' under '{task.name}'.")
            return f"created subtask {sub.id} (Pending; Mission Judgement will prioritise it)"

        async def remember(args: dict) -> str:
            content = sarg(args, "content")
            await MemoryRepo(self.pool).add(agent.id, swarm.id, content, team_id=team.id,
                                            task_id=task.id, embedding=self._embed(content))
            return "remembered"

        async def recall(args: dict) -> str:
            query = sarg(args, "query", required=False)
            mems = await MemoryRepo(self.pool).search(agent.id, self._embed(query), k=5)
            return "\n".join(f"- {m.content}" for m in mems) or "(no memories yet)"

        async def propose_team(args: dict) -> str:
            name = sarg(args, "name")
            mission = sarg(args, "mission_statement")
            reason = sarg(args, "reason", required=False)
            teams = await self.teams.list(swarm.id)
            if len(teams) >= swarm.config.limits.max_teams:
                raise ToolError(f"the swarm already has the maximum of "
                                f"{swarm.config.limits.max_teams} teams")
            wanted = name.lower()
            if any(t.name.lower() == wanted for t in teams) or any(
                    p.name.lower() == wanted and p.status in ("pending", "approved")
                    for p in await self.proposals.list(swarm.id)):
                raise ToolError(f"a team or proposal named {name!r} already exists")
            voters = await self._delivery_agents(swarm, team)
            prop = await self.proposals.create(
                swarm.id, team.id, agent.id, name, mission, reason,
                policy.majority_quorum(len(voters)))
            prop = await self.proposals.record_vote(prop.id, agent.id, True) or prop
            outcome = await self._settle(swarm, team, prop, len(voters))
            await self.events.add(
                swarm.id, "proposal", team_id=team.id, actor=agent.id,
                detail=f"{agent.name} proposed a new team '{name}' ({reason or 'no reason given'}); "
                       f"vote status: {outcome}.")
            await self.signal("proposal.created", team.id, task.id, proposal_id=prop.id)
            return f"proposal {prop.id} created; status {outcome}"

        async def vote_on_proposal(args: dict) -> str:
            pid = sarg(args, "proposal_id")
            approve = args.get("approve")
            if not isinstance(approve, bool):
                raise ToolError("'approve' must be true or false")
            prop = await self.proposals.get(pid)
            if prop is None or prop.swarm_id != swarm.id or prop.status != "pending":
                raise ToolError("no such pending proposal")
            if prop.team_id != team.id:
                raise ToolError("your team cannot vote on that proposal")
            prop = await self.proposals.record_vote(pid, agent.id, approve) or prop
            voters = await self._delivery_agents(swarm, team)
            outcome = await self._settle(swarm, team, prop, len(voters))
            await self.events.add(
                swarm.id, "proposal", team_id=team.id, actor=agent.id,
                detail=f"{agent.name} voted {'for' if approve else 'against'} the proposal for "
                       f"team '{prop.name}'; vote status: {outcome}.")
            return f"vote recorded; status {outcome}"

        async def submit(args: dict) -> str:
            submitted["summary"] = sarg(args, "summary", required=False)
            return "submitted for review"

        obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
        s = {"type": "string"}
        return [
            Tool("create_subtask", "Split off a smaller task under the current one. It starts "
                 "Pending until Mission Judgement approves it.",
                 obj({"name": s, "description": s, "artifact": {**s, "description":
                      "workspace path the subtask produces"},
                      "acceptance_criteria": {"type": "array", "items": s}}, ["name"]),
                 create_subtask),
            Tool("remember", "Save a short note to your persistent memory.",
                 obj({"content": s}, ["content"]), remember),
            Tool("recall", "Search your persistent memory.", obj({"query": s}), recall),
            Tool("propose_team", "Propose spinning out a new team for work outside this team's "
                 "mission. Your team's delivery agents vote (majority wins).",
                 obj({"name": s, "mission_statement": s, "reason": s},
                     ["name", "mission_statement"]), propose_team),
            Tool("vote_on_proposal", "Vote on a pending team proposal from your team.",
                 obj({"proposal_id": s, "approve": {"type": "boolean"}},
                     ["proposal_id", "approve"]), vote_on_proposal),
            Tool("submit", "Finish: submit your work for review with a short summary of what you "
                 "did and how each acceptance criterion is met.",
                 obj({"summary": s}, ["summary"]), submit, terminal=True),
        ]

    async def _settle(self, swarm: Swarm, team: Team, prop, n_voters: int) -> str:
        outcome = policy.tally(prop.votes, n_voters)
        if outcome != "pending":
            await self.proposals.set_status(prop.id, outcome)
        return outcome

    async def vote(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        """Each delivery agent that has not voted yet votes, one at a time, until decided."""
        prop = await self.proposals.get(action.proposal_id) if action.proposal_id else None
        team = await self.teams.get(prop.team_id) if prop is not None else None
        if (prop is None or prop.status != "pending" or team is None
                or team.status != TeamStatus.ACTIVE.value):
            return
        voters = sorted(await self._delivery_agents(swarm, team), key=lambda a: a.name)
        tasks = await self.tasks.list(team_id=team.id)
        done = [t for t in tasks if t.status == TaskStatus.DONE.value]
        open_ = [t for t in tasks if t.status not in (TaskStatus.DONE.value,
                                                      TaskStatus.FAILED.value)]
        model = escalate_model(choose_model(DELIVERY, swarm.config), swarm.config, escalation)
        for agent in voters:
            prop = await self.proposals.get(prop.id)
            if prop is None or prop.status != "pending":
                return
            if agent.id in prop.votes:
                continue
            approve, reason = await self._json_job(
                swarm, agent, team, "vote_proposal", model,
                prompts.vote_prompt(prop, team.name, open_, done), prompts.parse_vote,
                query=prop.name)
            prop = await self.proposals.record_vote(prop.id, agent.id, approve) or prop
            outcome = await self._settle(swarm, team, prop, len(voters))
            await self.events.add(
                swarm.id, "proposal", team_id=team.id, actor=agent.id,
                detail=f"{agent.name} voted {'for' if approve else 'against'} the proposal for "
                       f"team '{prop.name}': {_clip(reason, 250) or 'no reason given'} "
                       f"(vote status: {outcome}).")
            if outcome != "pending":
                await self.signal("proposal.decided", team.id, outcome=outcome,
                                  proposal_id=prop.id)
                return

    # -- review -----------------------------------------------------------------

    async def review_task(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        task = await self.tasks.get(action.task_id)
        if task is None or task.status != TaskStatus.REVIEW.value:
            return
        team = await self.teams.get(task.team_id)
        if team is None:
            return
        cfg = swarm.config
        ws = self.workspace(swarm)

        # 1. deterministic checks
        failed_checks: list[str] = []
        det_results: list[bool] = []
        for c in task.criteria:
            if not is_deterministic(c.check_kind):
                continue
            res = await asyncio.to_thread(run_check, c, ws)
            det_results.append(res.passed)
            if not res.passed:
                failed_checks.append(f"{c.text} ({res.evidence})")
            await self.tasks.update_criterion(
                c.id, CriterionStatus.GREEN.value if res.passed else CriterionStatus.RED.value,
                1.0 if res.passed else 0.0, res.evidence)
        task = await self.tasks.get(task.id) or task
        rubric = [c for c in task.criteria if not is_deterministic(c.check_kind)]
        files = read_deliverable_files(ws, review_paths(task))

        # 2. both judges (different agents, run concurrently)
        qj = await self._team_agent(swarm, team, QJ)
        mj = await self._team_agent(swarm, team, MJ)
        attempt = task.attempts + 1
        qkey, mkey = (task.id, attempt, "qj"), (task.id, attempt, "mj")

        async def quality():
            if qkey in self._partial:
                return self._partial[qkey]
            ordinals = [c.ordinal for c in rubric]
            model = escalate_model(choose_model(QJ, cfg), cfg, escalation)
            out = await self._json_job(
                swarm, qj, team, "review_quality", model,
                prompts.quality_review_prompt(task, task.criteria, files),
                lambda o: prompts.parse_quality_review(o, ordinals), task_id=task.id,
                query=task.name)
            self._partial[qkey] = out
            return out

        async def mission():
            if mkey in self._partial:
                return self._partial[mkey]
            model = escalate_model(choose_model(MJ, cfg), cfg, escalation)
            out = await self._json_job(
                swarm, mj, team, "review_mission", model,
                prompts.mission_review_prompt(task, (task.deliverable or {}).get("summary", ""),
                                              files),
                prompts.parse_mission_review, task_id=task.id, query=task.name)
            self._partial[mkey] = out
            return out

        results = await asyncio.gather(quality(), mission(), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]
        qr, mr = results
        try:
            await self._require_state(swarm)
        except SwarmNotActive:
            self._partial.pop(qkey, None)
            self._partial.pop(mkey, None)
            raise
        if await self._still(task.id, TaskStatus.REVIEW.value) is None:
            self._partial.pop(qkey, None)
            self._partial.pop(mkey, None)
            log.info("review of %r dropped: the task changed while the judges worked", task.name)
            return

        # 3. record: criteria, verdicts
        rubric_scores: list[float] = []
        for c in rubric:
            passed, score, reason = qr.criteria[c.ordinal]
            green = passed and score >= cfg.dod_thresholds.rubric
            rubric_scores.append(score)
            await self.tasks.update_criterion(
                c.id, CriterionStatus.GREEN.value if green else CriterionStatus.RED.value,
                score, reason)
        await self.verdicts.add(
            task.id, attempt, QJ, qj.id, qr.passed, qr.quality_score, qr.feedback,
            {"criteria": {str(k): {"passed": v[0], "score": v[1], "reason": v[2]}
                          for k, v in qr.criteria.items()}})
        await self.verdicts.add(
            task.id, attempt, MJ, mj.id, mr.expected_value_met, mr.confidence,
            mr.reasoning or mr.feedback, {"feedback": mr.feedback})
        await self.events.add(
            swarm.id, "verdict", team_id=team.id, task_id=task.id, actor=qj.id,
            detail=f"{qj.name} {'passed' if qr.passed else 'did not pass'} '{task.name}' "
                   f"(quality {qr.quality_score:.2f}, attempt {attempt}): "
                   f"{_clip(qr.feedback, 250) or 'no further comments'}")
        await self.events.add(
            swarm.id, "verdict", team_id=team.id, task_id=task.id, actor=mj.id,
            detail=f"{mj.name} says the expected value of '{task.name}' is "
                   f"{'met' if mr.expected_value_met else 'not met'} "
                   f"(confidence {mr.confidence:.2f}): {_clip(mr.reasoning or mr.feedback, 250)}")
        await self.signal("verdict.quality", team.id, task.id, passed=qr.passed,
                          score=qr.quality_score)
        await self.signal("verdict.mission", team.id, task.id, met=mr.expected_value_met,
                          confidence=mr.confidence)

        # 4. Definition of Done
        dod = policy.evaluate_dod(det_results, rubric_scores, qr.passed, mr.expected_value_met,
                                  mr.confidence, cfg.dod_thresholds)
        self._partial.pop(qkey, None)
        self._partial.pop(mkey, None)
        if dod.passed:
            await self.tasks.update(task.id, status=TaskStatus.DONE.value, done_at=_now(),
                                    attempts=attempt, feedback=None)
            await self.events.add(
                swarm.id, "done", team_id=team.id, task_id=task.id, actor="engine",
                from_status=TaskStatus.REVIEW.value, to_status=TaskStatus.DONE.value,
                detail=f"'{task.name}' is Done after {attempt} attempt(s): it met both the "
                       f"quality and mission Definition of Done (quality score "
                       f"{dod.quality_score:.2f}).")
            if attempt > 1 and task.feedback:
                await self.learnings.add(
                    f"Task '{task.name}' needed {attempt} attempts. Earlier judge feedback to "
                    f"avoid repeating: {_clip(task.feedback, 500)}", "pattern",
                    swarm_id=swarm.id, team_id=team.id, task_id=task.id,
                    embedding=self._embed(task.name))
            await self.signal("task.done", team.id, task.id)
            return

        feedback = combine_feedback(dod.reasons, task, qr, mr, failed_checks,
                                    cfg.dod_thresholds.rubric,
                                    cfg.dod_thresholds.mission_confidence)
        verdict = policy.rework_or_fail(attempt, cfg.limits.max_attempts_per_task)
        if verdict == "failed":
            await self.tasks.update(task.id, status=TaskStatus.FAILED.value, attempts=attempt,
                                    feedback=feedback)
            await self.events.add(
                swarm.id, "failed", team_id=team.id, task_id=task.id, actor="engine",
                from_status=TaskStatus.REVIEW.value, to_status=TaskStatus.FAILED.value,
                detail=f"'{task.name}' failed after {attempt} attempt(s). Last feedback: "
                       f"{_clip(feedback, 300)}")
            await self._learn_from_failure(swarm, team, task, feedback)
            await self.signal("task.failed", team.id, task.id)
        else:
            await self.tasks.update(task.id, status=TaskStatus.IN_PROGRESS.value,
                                    attempts=attempt, feedback=feedback)
            await self.events.add(
                swarm.id, "rework", team_id=team.id, task_id=task.id, actor="engine",
                from_status=TaskStatus.REVIEW.value, to_status=TaskStatus.IN_PROGRESS.value,
                detail=f"'{task.name}' needs rework (attempt {attempt} of "
                       f"{cfg.limits.max_attempts_per_task}): {_clip(feedback, 300)}")
            await self.signal("task.rework", team.id, task.id)

    async def _learn_from_failure(self, swarm: Swarm, team: Team, task: Task,
                                  feedback: str) -> None:
        await self.learnings.add(
            f"Task '{task.name}' failed. Judge feedback: {_clip(feedback, 500)}", "pattern",
            swarm_id=swarm.id, team_id=team.id, task_id=task.id,
            embedding=self._embed(task.name))

    # -- assessment -------------------------------------------------------------

    async def assess_mission(self, swarm: Swarm, action: Action, escalation: int = 0) -> None:
        team = await self.teams.get(action.team_id)
        if team is None or team.status != TeamStatus.ACTIVE.value:
            return
        tasks = await self.tasks.list(team_id=team.id)
        if policy.live_open_tasks(tasks):
            return
        done = [t for t in tasks if t.status == TaskStatus.DONE.value]
        failed = [t for t in tasks if t.status == TaskStatus.FAILED.value]
        mj = await self._team_agent(swarm, team, MJ)
        model = escalate_model(choose_model(MJ, swarm.config), swarm.config, escalation)
        # The assessment prompt only lists task names; show the judge the real deliverables.
        paths = dedupe_files(p for t in done for p in review_paths(t))
        ws = self.workspace(swarm)
        excerpts = read_deliverable_files(ws, paths[:ASSESS_MAX_FILES],
                                          limit=assess_clip(len(paths)))
        listing = workspace_listing(ws)
        # Every file that exists (a capped excerpt list once hid a README that was there).
        context = (f"All files in the shared workspace now:\n{listing}\n\n" if listing else "")
        if excerpts:
            context += ("Current deliverable files (excerpts):\n"
                        + "\n".join(f"--- {p} ---\n{c}\n" for p, c in excerpts.items()) + "\n")
        res = await self._json_job(
            swarm, mj, team, "assess_mission", model,
            context + prompts.assess_prompt(
                team, done, failed,
                [t for t in await self.teams.list(swarm.id) if t.id != team.id]),
            prompts.parse_assessment,
            query=team.mission_statement)
        threshold = swarm.config.dod_thresholds.mission_confidence
        now = _now()
        fields: dict[str, Any] = {"last_assessed_at": now}
        if res.next_focus:
            fields["next_focus"] = res.next_focus
        if res.status == "accomplished" and res.confidence >= threshold:
            fields["status"] = TeamStatus.ACCOMPLISHED.value
            await self.teams.update(team.id, **fields)
            await self.events.add(
                swarm.id, "assessment", team_id=team.id, actor=mj.id,
                detail=f"{mj.name} confirmed that team '{team.name}' accomplished its mission "
                       f"(confidence {res.confidence:.2f}): {_clip(res.reasoning, 300)}")
            await self.signal("team.accomplished", team_id=team.id)
        elif res.status == "restart":
            fields["generation"] = team.generation + 1
            await self.teams.update(team.id, **fields)
            await self.learnings.add(
                f"Team '{team.name}' restarted its approach. Why: {_clip(res.reasoning, 400)} "
                f"Next focus: {_clip(res.next_focus, 200)}", "pattern",
                swarm_id=swarm.id, team_id=team.id, created_by=mj.id,
                embedding=self._embed(team.mission_statement))
            await self.events.add(
                swarm.id, "assessment", team_id=team.id, actor=mj.id,
                detail=f"{mj.name} restarted team '{team.name}' (generation "
                       f"{team.generation + 1}): {_clip(res.reasoning, 300)}")
        else:
            await self.teams.update(team.id, **fields)
            note = ("" if res.status == "continue" else
                    f" It said 'accomplished' but only with confidence {res.confidence:.2f}, "
                    f"below the required {threshold:.2f}.")
            await self.events.add(
                swarm.id, "assessment", team_id=team.id, actor=mj.id,
                detail=f"{mj.name} assessed team '{team.name}': more work needed.{note} "
                       f"{_clip(res.reasoning, 250)}")
