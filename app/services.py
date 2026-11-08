"""Shared application services used by both the API (app.main) and the engine (app.engine).

Kept deliberately small: anything that both processes must do identically lives
here, so team creation, agent rosters and workspace paths cannot drift.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Optional

import asyncpg

from app.config import Settings
from app.models import Agent, AgentRole, AgentState, CreatorKind, Swarm, TaskStatus, Team
from app.prompts import compose_system_prompt, parse_standards, standards_prompt
from app.repo import (AgentRepo, CallRepo, ConfigRepo, EventRepo, FeedbackRepo, LearningRepo,
                      MemoryRepo, TeamRepo)
from app.routing import choose_model

#: Display names for the fixed team roles (Delivery agents are numbered).
ROLE_NAMES = {
    AgentRole.MISSION_JUDGEMENT.value: "Mission Judgement",
    AgentRole.QUALITY_JUDGEMENT.value: "Quality Judgement",
    AgentRole.SWARM_COORDINATOR.value: "Swarm Coordinator",
}


# -- workspaces ---------------------------------------------------------------

def workspace_dir(settings: Settings, swarm_id: str) -> Path:
    """Artifact directory of a swarm. Swarm ids are uuid4 strings (no path parts)."""
    if not swarm_id or "/" in swarm_id or "\\" in swarm_id or swarm_id in (".", ".."):
        raise ValueError(f"invalid swarm id for a workspace: {swarm_id!r}")
    return Path(settings.WORKSPACE_ROOT) / swarm_id


def ensure_workspace(settings: Settings, swarm_id: str) -> Path:
    path = workspace_dir(settings, swarm_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def remove_workspace(settings: Settings, swarm_id: str) -> None:
    path = workspace_dir(settings, swarm_id)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


# -- rosters --------------------------------------------------------------------

async def ensure_coordinator(pool: asyncpg.Pool, swarm: Swarm):
    """The swarm's single Swarm Coordinator agent (created on first call)."""
    agents = AgentRepo(pool)
    for a in await agents.list(swarm.id):
        if a.role == AgentRole.SWARM_COORDINATOR.value:
            return a
    spec = swarm.config.coordinator
    return await agents.create(
        swarm.id, None, AgentRole.SWARM_COORDINATOR.value,
        ROLE_NAMES[AgentRole.SWARM_COORDINATOR.value],
        model=choose_model(AgentRole.SWARM_COORDINATOR.value, swarm.config),
        harness=spec.harness, provider=spec.provider)


async def ensure_team_agents(pool: asyncpg.Pool, swarm: Swarm, team: Team) -> list:
    """Idempotently give a team its README roster: 1 MJ, 1 QJ, N Delivery agents."""
    agents = AgentRepo(pool)
    existing = await agents.list(swarm.id, team_id=team.id)
    have = {(a.role, a.name) for a in existing}
    cfg = swarm.config
    wanted: list[tuple[str, str]] = [
        (AgentRole.MISSION_JUDGEMENT.value, ROLE_NAMES[AgentRole.MISSION_JUDGEMENT.value]),
        (AgentRole.QUALITY_JUDGEMENT.value, ROLE_NAMES[AgentRole.QUALITY_JUDGEMENT.value]),
    ] + [(AgentRole.MISSION_DELIVERY.value, f"Delivery-{i}")
         for i in range(1, cfg.delivery_agents_per_team + 1)]
    created = []
    for role, name in wanted:
        if (role, name) in have:
            continue
        created.append(await agents.create(
            swarm.id, team.id, role, name, model=choose_model(role, cfg),
            harness=cfg.allowed_harnesses[0], provider=cfg.allowed_providers[0]))
    return existing + created


async def create_team(pool: asyncpg.Pool, swarm: Swarm, name: str, mission_statement: str,
                      creator_kind: str = CreatorKind.USER.value,
                      creator_team_id: Optional[str] = None,
                      actor: str = "user") -> Team:
    """Create a team, its agents, and a human-readable event."""
    team = await TeamRepo(pool).create(swarm.id, name, mission_statement,
                                       creator_kind=creator_kind,
                                       creator_team_id=creator_team_id)
    await ensure_team_agents(pool, swarm, team)
    origin = {"user": "by the user", "swarm_coordinator": "by the Swarm Coordinator",
              "team": "by majority vote of another team"}.get(creator_kind, "")
    await EventRepo(pool).add(swarm.id, "team", team_id=team.id, actor=actor,
                              detail=f"Team '{name}' created {origin}. Mission: {mission_statement}")
    return team


# -- LLM ------------------------------------------------------------------------

def build_llm(settings: Settings):
    """The native "daash" harness client for the configured OpenAI-compatible gateway."""
    from app.llm import LLMClient  # local import: keeps services importable in pure tests

    return LLMClient(settings.LLM_ENDPOINT, api_key=settings.LLM_API_KEY,
                     timeout=settings.LLM_TIMEOUT_SECONDS, max_tokens=settings.LLM_MAX_TOKENS,
                     default_concurrency=settings.LLM_MAX_CONCURRENCY,
                     model_concurrency=settings.model_concurrency())


# -- prompt context + accounting -------------------------------------------------

async def agent_system_prompt(pool: asyncpg.Pool, swarm: Swarm, agent: Agent,
                              team: Optional[Team] = None, query: str = "",
                              embedder: Any = None) -> str:
    """The deterministic Vision → Values → Mission → DoD/DoR → Role → context prompt."""
    global_cfg = await ConfigRepo(pool).get_global()
    feedback = [f.text for f in await FeedbackRepo(pool).list(swarm.id)]
    emb = embedder.embed(query) if (embedder is not None and query) else None
    learnings = [l.content for l in await LearningRepo(pool).search(emb, swarm.id, k=8)]
    memories = [m.content for m in await MemoryRepo(pool).search(agent.id, emb, k=8)]
    return compose_system_prompt(
        vision=swarm.vision_statement, role=agent.role,
        values_principles=global_cfg.values_principles,
        mission=team.mission_statement if team else None,
        team_name=team.name if team else None,
        vision_dod=swarm.definition_of_done,
        team_dor=team.definition_of_ready if team else None,
        team_dod=team.definition_of_done if team else None,
        feedback=feedback, learnings=learnings, memories=memories)


async def record_call(pool: asyncpg.Pool, agent: Agent, purpose: str, model: str, *,
                      usage: Any = None, transcript: Optional[list] = None,
                      duration_ms: int = 0, ok: bool = True, error: Optional[str] = None,
                      task_id: Optional[str] = None):
    """Persist one LLM job (token accounting + transcript) and bump the agent's counters."""
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    total = getattr(usage, "total_tokens", 0) or (prompt + completion)
    call = await CallRepo(pool).add(
        agent.swarm_id, agent.role, model, purpose, team_id=agent.team_id, agent_id=agent.id,
        task_id=task_id, prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
        duration_ms=duration_ms, ok=ok, error=(error or None) and str(error)[:2000],
        transcript=transcript or [])
    await AgentRepo(pool).record_usage(agent.id, total, ok)
    return call


async def team_agent(pool: asyncpg.Pool, swarm: Swarm, team: Team, role: str) -> Agent:
    """First agent of `role` in the team (creating the roster if needed)."""
    agents = await ensure_team_agents(pool, swarm, team)
    for a in sorted(agents, key=lambda a: a.name):
        if a.role == role:
            return a
    raise LookupError(f"team {team.id} has no {role} agent")


VERIFY_CLOSE_REASON = "Closed: the swarm went to verification before this task finished."


async def close_open_tasks(pool: asyncpg.Pool, swarm_id: str, reason: str) -> int:
    """Reject every open task (Pending/Ready/In-Progress/Review) of the swarm.

    Used when the swarm leaves ACTIVE for verification, so nothing stays In-Progress forever.
    Clears the assignee, and writes one summary event. Returns how many tasks were closed."""
    rows = await pool.fetch(
        "UPDATE tasks SET status = 'Rejected', rejection_reason = $2, assigned_agent_id = NULL, "
        "updated_at = now() WHERE swarm_id = $1 AND status = ANY($3::text[]) RETURNING id",
        swarm_id, reason, list(TaskStatus.open_statuses()))
    n = len(rows)
    if n:
        await EventRepo(pool).add(
            swarm_id, "rejected", actor="engine",
            detail=f"{n} open task(s) closed as Rejected. {reason}")
    return n


async def generate_standards(pool: asyncpg.Pool, llm: Any, swarm: Swarm, team: Team,
                             dor_guidance: str = "", dod_guidance: str = "",
                             embedder: Any = None, manage_state: bool = True,
                             still_ok: Any = None) -> Team:
    """Quality Judgement derives the team's DoR additions and team-mission DoD.

    Raises app.llm.LLMError on harness/parse failure (after recording the call).
    """
    from app.llm import LLMError

    qj = await team_agent(pool, swarm, team, AgentRole.QUALITY_JUDGEMENT.value)
    model = choose_model(AgentRole.QUALITY_JUDGEMENT.value, swarm.config)
    system = await agent_system_prompt(pool, swarm, qj, team, query=team.mission_statement,
                                       embedder=embedder)
    agents = AgentRepo(pool)
    if manage_state:
        await agents.set_state(qj.id, AgentState.BUSY.value)
    t0 = time.monotonic()
    try:
        standards, loop = await llm.complete_json(
            model, system, standards_prompt(dor_guidance, dod_guidance), parse_standards)
    except LLMError as exc:
        await record_call(pool, qj, "generate_standards", model, usage=exc.usage,
                          transcript=exc.transcript, ok=False, error=str(exc),
                          duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    finally:
        if manage_state:
            await agents.set_state(qj.id, AgentState.IDLE.value)
    await record_call(pool, qj, "generate_standards", model, usage=loop.usage,
                      transcript=loop.transcript,
                      duration_ms=int((time.monotonic() - t0) * 1000))
    if still_ok is not None and not await still_ok():
        return team  # the swarm left ACTIVE while the call ran: discard the result
    updated = await TeamRepo(pool).update(
        team.id, definition_of_ready=standards["definition_of_ready"],
        definition_of_done=standards["definition_of_done"])
    guided = " (with user guidance)" if (dor_guidance or dod_guidance) else ""
    await EventRepo(pool).add(
        swarm.id, "team", team_id=team.id, actor=qj.id,
        detail=f"Quality Judgement set the Definition of Ready and Definition of Done "
               f"for team '{team.name}'{guided}.")
    return updated or team
