"""DAASH coordinator HTTP API (docs/DESIGN-0.1.md section 9). JSON throughout.

REST only: no agent work happens here (that is the engine, ``python -m app.engine``).
Process-wide handles (``pool``, ``bus``, ``llm``, ``embedder``, ``settings``) are module
globals created by the lifespan; tests may replace them with fakes before issuing requests.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import api_helpers as H
from app import help as help_pages
from app import remote as R
from app import services
from app.bus import DecisionBus, ProposalError
from app.config import Settings
from app.db import create_pool
from app.embeddings import build_embedder
from app.llm import LLMError
from app.metrics import compute_metrics
from app.models import (
    FeedbackCreate, GlobalConfig, QualityGenerateRequest, RemoteJob, Runner, Swarm, SwarmCreate, SwarmStatus,
    SwarmUpdate, TaskCreate, TaskUpdate, TeamCreate, TeamUpdate,
)
from app.policy import transition as policy_transition
from app.repo import (
    AgentRepo, CallRepo, ConfigRepo, EventRepo, FeedbackRepo, LearningRepo, ProposalRepo,
    RemoteJobRepo, RunnerRepo, SwarmRepo, TaskRepo, TeamRepo, VerdictRepo,
)
from app.routing import validate_config

log = logging.getLogger(__name__)

settings = Settings()
pool: Optional[asyncpg.Pool] = None
bus: DecisionBus = DecisionBus(settings.REDIS_URL, ttl_seconds=settings.BUS_TTL_SECONDS,
                               signals_max=settings.BUS_SIGNALS_MAX)
llm: Any = services.build_llm(settings)
embedder: Any = build_embedder(settings.EMBEDDING_MODEL, settings.EMBEDDING_USE_MODEL)
BOOTSTRAP_RUNNER = "local-runner"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, bus, llm, embedder
    pool = await create_pool(settings.DATABASE_URL)
    bus = DecisionBus(settings.REDIS_URL, ttl_seconds=settings.BUS_TTL_SECONDS,
                      signals_max=settings.BUS_SIGNALS_MAX)
    llm = services.build_llm(settings)
    embedder = build_embedder(settings.EMBEDDING_MODEL, settings.EMBEDDING_USE_MODEL)
    if settings.RUNNER_BOOTSTRAP_TOKEN:
        await RunnerRepo(pool).ensure_bootstrap(
            BOOTSTRAP_RUNNER, R.hash_token(settings.RUNNER_BOOTSTRAP_TOKEN))
    if not settings.API_TOKEN:
        log.warning("DAASH_API_TOKEN is not set: the admin API and DAASHboard are OPEN. "
                    "Set it before exposing the coordinator beyond this machine.")
    log.info("Coordinator ready (LLM endpoint %s).", settings.LLM_ENDPOINT)
    try:
        yield
    finally:
        await bus.close()
        await pool.close()
        pool = None


# -- authentication (docs/DESIGN-0.3.md section 4) ------------------------------------------------

_WWW_AUTH = {"WWW-Authenticate": "Bearer"}


async def require_admin(request: Request) -> None:
    """Admin bearer auth for every endpoint except /health, /help/* (online help with sample
    data only) and /runner/* (runner tokens). Open when DAASH_API_TOKEN is empty (local
    prototype)."""
    expected = settings.API_TOKEN
    if not expected:
        return
    path = request.url.path
    if path == "/health" or path.startswith(("/runner/", "/help/")):
        return
    if not R.tokens_match(R.bearer_token(request.headers.get("authorization")), expected):
        raise HTTPException(401, "Authentication required", headers=_WWW_AUTH)


async def runner_auth(request: Request) -> Runner:
    """The enabled runner owning the bearer token, else 401. Every authenticated call counts
    as being seen."""
    token = R.bearer_token(request.headers.get("authorization"))
    runner = await RunnerRepo(_p()).by_token_hash(R.hash_token(token)) if token else None
    if (runner is None or not runner.enabled
            or not R.tokens_match(R.hash_token(token), runner.token_hash)):
        raise HTTPException(401, "Invalid runner token", headers=_WWW_AUTH)
    await RunnerRepo(_p()).touch(runner.id)
    return runner


app = FastAPI(title="DAASH", lifespan=lifespan, dependencies=[Depends(require_admin)])
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# -- helpers ------------------------------------------------------------------

def _p() -> asyncpg.Pool:
    if pool is None:
        raise HTTPException(503, "database not ready")
    return pool


def _j(obj: Any) -> Any:
    """JSON-ready form of a model / list of models."""
    if isinstance(obj, list):
        return [_j(o) for o in obj]
    return obj.model_dump(mode="json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _swarm_or_404(swarm_id: str) -> Swarm:
    swarm = await SwarmRepo(_p()).get(swarm_id)
    if swarm is None:
        raise HTTPException(404, "swarm not found")
    return swarm


async def _team_or_404(team_id: str):
    team = await TeamRepo(_p()).get(team_id)
    if team is None:
        raise HTTPException(404, "team not found")
    return team


async def _task_or_404(task_id: str):
    task = await TaskRepo(_p()).get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


async def _swarm_detail(swarm: Swarm) -> dict:
    teams = await TeamRepo(_p()).list(swarm.id)
    agents = await AgentRepo(_p()).list(swarm.id)
    return {**_j(swarm), "teams": _j(teams), "agents": _j(agents)}


def _check_config(cfg) -> None:
    errors = validate_config(cfg)
    if errors:
        raise HTTPException(400, "Invalid swarm config: " + "; ".join(errors))


async def _hard_delete(swarm: Swarm) -> None:
    await SwarmRepo(_p()).delete(swarm.id)
    services.remove_workspace(settings, swarm.id)


# -- health / config / models ------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "auth_required": bool(settings.API_TOKEN)}


# -- online help (DAASHboard) --------------------------------------------------------------

@app.get("/help/prompts", response_class=HTMLResponse)
async def help_prompts() -> HTMLResponse:
    """How DAASH prompts its agents, rendered live from app/prompts.py with sample data."""
    return HTMLResponse(help_pages.prompts_page(full_document=True))


@app.get("/config")
async def get_config() -> dict:
    return _j(await ConfigRepo(_p()).get_global())


@app.put("/config")
async def put_config(cfg: GlobalConfig) -> dict:
    _check_config(cfg.default_swarm_config)
    await ConfigRepo(_p()).put_global(cfg)
    return _j(await ConfigRepo(_p()).get_global())


@app.get("/models")
async def get_models() -> dict:
    try:
        models = await asyncio.wait_for(llm.list_models(), timeout=5)
        return {"models": models, "endpoint": settings.LLM_ENDPOINT, "reachable": True}
    except Exception as exc:  # noqa: BLE001 - never a 500
        log.warning("model listing failed: %r", exc)
        return {"models": [], "endpoint": settings.LLM_ENDPOINT, "reachable": False}


# -- swarms -----------------------------------------------------------------------------

@app.get("/swarms")
async def list_swarms() -> dict:
    return {"swarms": _j(await SwarmRepo(_p()).list())}


@app.get("/swarms/active")
async def get_active_swarm():
    swarm = await SwarmRepo(_p()).get_live()
    return None if swarm is None else await _swarm_detail(swarm)


@app.post("/swarms", status_code=201)
async def create_swarm(req: SwarmCreate) -> dict:
    repo = SwarmRepo(_p())
    cfg = req.config or (await ConfigRepo(_p()).get_global()).default_swarm_config
    _check_config(cfg)
    live = await repo.get_live()
    if live is not None:
        raise HTTPException(409, f"A live swarm already exists: '{live.name}' "
                                 f"({live.lifecycle_state}). Archive or delete it first.")
    try:
        swarm = await repo.create(req.name, req.vision_statement, req.definition_of_done, cfg)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, "A live swarm already exists. Archive or delete it first.")
    try:
        services.ensure_workspace(settings, swarm.id)
        for t in req.teams:
            await services.create_team(_p(), swarm, t.name, t.mission_statement)
        await services.ensure_coordinator(_p(), swarm)
        await EventRepo(_p()).add(swarm.id, "swarm", actor="user",
                                  detail=f"Swarm '{swarm.name}' created.")
    except Exception:
        await _hard_delete(swarm)
        raise
    return await _swarm_detail(swarm)


@app.get("/swarms/{swarm_id}")
async def get_swarm(swarm_id: str) -> dict:
    return await _swarm_detail(await _swarm_or_404(swarm_id))


@app.patch("/swarms/{swarm_id}")
async def update_swarm(swarm_id: str, req: SwarmUpdate) -> dict:
    swarm = await _swarm_or_404(swarm_id)
    fields = req.model_dump(exclude_unset=True)
    if fields.get("vision_statement") is not None and \
            swarm.lifecycle_state != SwarmStatus.CREATED.value:
        raise HTTPException(409, "The vision can only be changed while the swarm is CREATED.")
    if fields.get("config") is not None:
        _check_config(req.config)
    fields = {k: v for k, v in fields.items() if v is not None}
    if "config" in fields:
        fields["config"] = req.config
    if fields:
        swarm = await SwarmRepo(_p()).update(swarm_id, **fields) or swarm
    return await _swarm_detail(swarm)


@app.post("/swarms/{swarm_id}/transitions/{action}")
async def transition_swarm(swarm_id: str, action: str) -> dict:
    swarm = await _swarm_or_404(swarm_id)
    try:
        new_state = policy_transition(swarm.lifecycle_state, action)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if action == "delete":
        await _hard_delete(swarm)
        return {"deleted": True, "id": swarm_id}
    fields = H.transition_fields(action, new_state, swarm.started_at, _now())
    updated = await SwarmRepo(_p()).update(swarm_id, **fields)
    await EventRepo(_p()).add(swarm_id, "swarm", actor="user",
                              detail=H.transition_detail(action, swarm.lifecycle_state, new_state))
    if action == "verify":
        await services.close_open_tasks(_p(), swarm_id, services.VERIFY_CLOSE_REASON)
    return await _swarm_detail(updated or swarm)


@app.post("/swarms/{swarm_id}/feedback", status_code=201)
async def post_feedback(swarm_id: str, req: FeedbackCreate) -> dict:
    swarm = await _swarm_or_404(swarm_id)
    fb = await FeedbackRepo(_p()).add(swarm_id, req.text)
    await EventRepo(_p()).add(swarm_id, "feedback", actor="user",
                              detail=f"User feedback: {req.text}")
    if swarm.lifecycle_state == SwarmStatus.PAUSED.value:
        new_state = policy_transition(swarm.lifecycle_state, "feedback")
        swarm = await SwarmRepo(_p()).update(swarm_id, lifecycle_state=new_state) or swarm
        await EventRepo(_p()).add(swarm_id, "swarm", actor="user",
                                  detail=H.transition_detail("feedback", "PAUSED", new_state))
    return {**_j(fb), "lifecycle_state": swarm.lifecycle_state}


@app.get("/swarms/{swarm_id}/feedback")
async def list_feedback(swarm_id: str) -> dict:
    await _swarm_or_404(swarm_id)
    return {"feedback": _j(await FeedbackRepo(_p()).list(swarm_id))}


@app.delete("/swarms/{swarm_id}")
async def delete_swarm(swarm_id: str) -> dict:
    swarm = await _swarm_or_404(swarm_id)
    if swarm.lifecycle_state not in ("CREATED", "VERIFICATION", "ARCHIVED"):
        raise HTTPException(409, f"Cannot delete a swarm in state {swarm.lifecycle_state}; "
                                 f"only CREATED, VERIFICATION or ARCHIVED swarms can be deleted.")
    await _hard_delete(swarm)
    return {"deleted": True, "id": swarm_id}


@app.get("/swarms/{swarm_id}/board")
async def get_board(swarm_id: str) -> dict:
    swarm = await _swarm_or_404(swarm_id)
    teams = await TeamRepo(_p()).list(swarm_id)
    agents = await AgentRepo(_p()).list(swarm_id)
    tasks = await TaskRepo(_p()).list(swarm_id=swarm_id)
    coordinator = next((a for a in agents if a.team_id is None
                        and a.role == "swarm_coordinator"), None)
    return {
        "swarm": _j(swarm),
        "coordinator": _j(coordinator) if coordinator else None,
        "teams": [{"team": _j(t),
                   "agents": _j([a for a in agents if a.team_id == t.id]),
                   "tasks": _j([x for x in tasks if x.team_id == t.id])} for t in teams],
    }


@app.get("/swarms/{swarm_id}/metrics")
async def get_metrics(swarm_id: str) -> dict:
    swarm = await _swarm_or_404(swarm_id)
    now = _now()
    tasks = await TaskRepo(_p()).list(swarm_id=swarm_id)
    events = await EventRepo(_p()).list_since(swarm_id, None)
    calls = await CallRepo(_p()).list_for_swarm(swarm_id)
    teams = await TeamRepo(_p()).list(swarm_id)
    m = compute_metrics(tasks, events, calls, teams, now, prices=swarm.config.model_prices)
    m["limits"] = H.limits_block(swarm.config.limits, swarm.started_at,
                                 m["tokens"]["total"], m["cost_usd"], now)
    return m


@app.get("/swarms/{swarm_id}/events")
async def list_events(swarm_id: str, limit: int = 100, team_id: Optional[str] = None,
                      task_id: Optional[str] = None) -> dict:
    await _swarm_or_404(swarm_id)
    limit = max(1, min(limit, 1000))
    return {"events": _j(await EventRepo(_p()).list(swarm_id, limit=limit, team_id=team_id,
                                                    task_id=task_id))}


@app.get("/swarms/{swarm_id}/artifacts")
async def list_artifacts(swarm_id: str) -> dict:
    await _swarm_or_404(swarm_id)
    root = services.workspace_dir(settings, swarm_id)
    return {"files": await asyncio.to_thread(H.list_artifacts, root)}


@app.get("/swarms/{swarm_id}/artifacts/{path:path}")
async def read_artifact(swarm_id: str, path: str) -> dict:
    await _swarm_or_404(swarm_id)
    root = services.workspace_dir(settings, swarm_id)
    try:
        return await asyncio.to_thread(H.read_artifact, root, path)
    except FileNotFoundError:
        raise HTTPException(404, "artifact not found")


@app.get("/swarms/{swarm_id}/learnings")
async def list_learnings(swarm_id: str) -> dict:
    await _swarm_or_404(swarm_id)
    return {"learnings": _j(await LearningRepo(_p()).list(swarm_id))}


@app.get("/swarms/{swarm_id}/proposals")
async def list_swarm_proposals(swarm_id: str) -> dict:
    await _swarm_or_404(swarm_id)
    return {"proposals": _j(await ProposalRepo(_p()).list(swarm_id))}


@app.get("/agents/{agent_id}/calls")
async def list_agent_calls(agent_id: str, limit: int = 20) -> dict:
    if await AgentRepo(_p()).get(agent_id) is None:
        raise HTTPException(404, "agent not found")
    return {"calls": _j(await CallRepo(_p()).list_for_agent(agent_id, limit=max(1, min(limit, 200))))}


# -- teams ---------------------------------------------------------------------------------

@app.get("/teams")
async def list_teams(swarm_id: Optional[str] = None) -> dict:
    if swarm_id is None:
        swarm = await SwarmRepo(_p()).get_live()
        if swarm is None:
            return {"teams": []}
        swarm_id = swarm.id
    return {"teams": _j(await TeamRepo(_p()).list(swarm_id))}


@app.post("/teams", status_code=201)
async def create_team(req: TeamCreate) -> dict:
    swarm = await _swarm_or_404(req.swarm_id)
    if swarm.lifecycle_state in SwarmStatus.terminal():
        raise HTTPException(409, f"Cannot add a team to a {swarm.lifecycle_state} swarm.")
    team = await services.create_team(_p(), swarm, req.name, req.mission_statement)
    agents = await AgentRepo(_p()).list(swarm.id, team_id=team.id)
    return {**_j(team), "agents": _j(agents)}


@app.get("/teams/{team_id}")
async def get_team(team_id: str) -> dict:
    team = await _team_or_404(team_id)
    return {**_j(team), "agents": _j(await AgentRepo(_p()).list(team.swarm_id, team_id=team.id))}


@app.patch("/teams/{team_id}")
async def update_team(team_id: str, req: TeamUpdate) -> dict:
    await _team_or_404(team_id)
    fields = {k: (v.value if hasattr(v, "value") else v)
              for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    team = await TeamRepo(_p()).update(team_id, **fields) if fields else await _team_or_404(team_id)
    return _j(team)


@app.delete("/teams/{team_id}")
async def delete_team(team_id: str) -> dict:
    team = await _team_or_404(team_id)
    await TeamRepo(_p()).delete(team_id)
    await EventRepo(_p()).add(team.swarm_id, "team", actor="user",
                              detail=f"Team '{team.name}' deleted by the user.")
    return {"deleted": True, "id": team_id}


@app.get("/teams/{team_id}/quality")
async def get_team_quality(team_id: str) -> dict:
    team = await _team_or_404(team_id)
    return {"team_id": team.id, "definition_of_ready": team.definition_of_ready,
            "definition_of_done": team.definition_of_done}


@app.post("/teams/{team_id}/quality", status_code=201)
async def generate_team_quality(
        team_id: str, req: Optional[QualityGenerateRequest] = Body(default=None)) -> dict:
    req = req or QualityGenerateRequest()
    team = await _team_or_404(team_id)
    swarm = await _swarm_or_404(team.swarm_id)
    try:
        team = await services.generate_standards(
            _p(), llm, swarm, team, dor_guidance=req.dor_guidance,
            dod_guidance=req.dod_guidance, embedder=embedder)
    except LLMError as exc:
        raise HTTPException(502, f"Quality Judgement could not generate standards: {exc}")
    return {"team_id": team.id, "definition_of_ready": team.definition_of_ready,
            "definition_of_done": team.definition_of_done}


# -- tasks -------------------------------------------------------------------------------------

@app.get("/tasks")
async def list_tasks(swarm_id: Optional[str] = None, team_id: Optional[str] = None,
                     status: Optional[str] = None) -> dict:
    if swarm_id is None and team_id is None:
        live = await SwarmRepo(_p()).get_live()
        if live is None:
            return {"tasks": []}
        swarm_id = live.id
    return {"tasks": _j(await TaskRepo(_p()).list(swarm_id=swarm_id, team_id=team_id,
                                                 status=status))}


@app.post("/tasks", status_code=201)
async def create_task(req: TaskCreate) -> dict:
    team = await _team_or_404(req.team_id)
    problem = H.criteria_error(req.acceptance_criteria)
    if not problem and not req.acceptance_criteria:
        problem = ("A task needs at least one acceptance criterion: the Definition of Ready "
                   "never commits a task without one.")
    if problem:
        raise HTTPException(422, problem)
    emb = None
    try:
        emb = await asyncio.to_thread(embedder.embed, f"{req.name}\n{req.description}")
    except Exception as exc:  # noqa: BLE001 - embedding is an optimisation, not a requirement
        log.warning("task embedding failed: %r", exc)
    task = await TaskRepo(_p()).create(team.swarm_id, req, created_by="user", embedding=emb)
    await EventRepo(_p()).add(team.swarm_id, "created", team_id=team.id, task_id=task.id,
                              to_status=task.status, actor="user",
                              detail=f"User created task '{task.name}'.")
    return _j(task)


@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = await _task_or_404(task_id)
    verdicts = await VerdictRepo(_p()).list_for_task(task_id)
    events = await EventRepo(_p()).list(task.swarm_id, limit=200, task_id=task_id)
    children = await TaskRepo(_p()).children(task_id)
    return {**_j(task), "verdicts": _j(verdicts), "events": _j(events), "children": _j(children)}


_STATUS_EVENT_KIND = {"Rejected": "rejected", "Done": "done", "Failed": "failed",
                      "Ready": "approved", "In-Progress": "committed", "Review": "submitted",
                      "Pending": "rework"}


@app.patch("/tasks/{task_id}")
async def update_task(task_id: str, req: TaskUpdate) -> dict:
    task = await _task_or_404(task_id)
    fields = H.task_patch_fields(req.model_dump(exclude_unset=True))
    if not fields:
        return _j(task)
    updated = await TaskRepo(_p()).update(task_id, **fields)
    new_status = fields.get("status")
    changed = ", ".join(sorted(k for k in fields if k not in
                               ("assigned_agent_id", "approved_at", "committed_at", "done_at")))
    if new_status and new_status != task.status:
        detail = f"User moved task '{task.name}' from {task.status} to {new_status}."
        if fields.get("rejection_reason"):
            detail += f" Reason: {fields['rejection_reason']}"
        kind = _STATUS_EVENT_KIND.get(new_status, "feedback")
    else:
        detail = f"User edited task '{task.name}' ({changed})."
        kind = "feedback"
    await EventRepo(_p()).add(task.swarm_id, kind, team_id=task.team_id, task_id=task_id,
                              from_status=task.status if new_status else None,
                              to_status=new_status, actor="user", detail=detail)
    return _j(updated)


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str) -> None:
    task = await _task_or_404(task_id)
    await TaskRepo(_p()).delete(task_id)
    await EventRepo(_p()).add(task.swarm_id, "feedback", team_id=task.team_id, actor="user",
                              detail=f"User deleted task '{task.name}'.")


# -- Decision Bus (unchanged) --------------------------------------------------------------

class SignalPublishRequest(BaseModel):
    kind: str = Field(..., min_length=1, max_length=100)
    team_id: Optional[str] = None
    task_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)


class ProposalCreateRequest(BaseModel):
    team_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    quorum: int = Field(1, ge=1)
    task_id: Optional[str] = None


class VoteRequest(BaseModel):
    voter: str = Field(..., min_length=1)
    approve: bool


@app.post("/decisions", status_code=201)
async def publish_decision(req: SignalPublishRequest) -> dict:
    signal = await bus.publish(req.kind, team_id=req.team_id, task_id=req.task_id,
                               payload=req.payload)
    return signal.model_dump()


@app.get("/decisions")
async def list_decisions(limit: int = 50) -> dict:
    signals = await bus.recent_signals(limit=limit)
    return {"signals": [s.model_dump() for s in signals]}


@app.post("/proposals", status_code=201)
async def create_proposal(req: ProposalCreateRequest) -> dict:
    proposal = await bus.create_proposal(req.team_id, req.question, quorum=req.quorum,
                                         task_id=req.task_id)
    return proposal.model_dump()


@app.get("/proposals")
async def list_proposals(team_id: Optional[str] = None) -> dict:
    proposals = await bus.list_proposals(team_id=team_id)
    return {"proposals": [p.model_dump() for p in proposals]}


@app.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: str) -> dict:
    proposal = await bus.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(404, "proposal not found")
    return proposal.model_dump()


@app.post("/proposals/{proposal_id}/votes")
async def cast_vote(proposal_id: str, req: VoteRequest) -> dict:
    try:
        proposal = await bus.cast_vote(proposal_id, voter=req.voter, approve=req.approve)
    except ProposalError as exc:
        status = {"notfound": 404, "resolved": 409, "duplicate": 409}[exc.code]
        raise HTTPException(status, str(exc))
    signal_kind = f"proposal_{proposal.status}" if proposal.status != "pending" else "proposal_vote"
    await bus.publish(signal_kind, team_id=proposal.team_id, task_id=proposal.task_id,
                      payload={"proposal_id": proposal.id, "voter": req.voter,
                               "vote": "approve" if req.approve else "reject",
                               "status": proposal.status})
    return proposal.model_dump()


# -- runners: admin management (docs/DESIGN-0.3.md section 4) ------------------------------------------

class RunnerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class RunnerPatchRequest(BaseModel):
    enabled: bool


def _runner_json(r: Runner, active: dict[str, int], now: datetime) -> dict:
    online = bool(r.enabled and r.last_seen_at is not None
                  and (now - r.last_seen_at).total_seconds() <= R.ONLINE_SECONDS)
    return {**r.model_dump(mode="json"), "online": online,
            "active_jobs": active.get(r.id, 0)}


async def _runner_view(r: Runner) -> dict:
    return _runner_json(r, await RunnerRepo(_p()).active_job_counts(), _now())


@app.post("/runners", status_code=201)
async def create_runner(req: RunnerCreateRequest) -> dict:
    name = req.name.strip()
    if not name:
        raise HTTPException(422, "name must not be blank")
    token = R.new_runner_token()
    try:
        runner = await RunnerRepo(_p()).create(name, R.hash_token(token))
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"A runner named '{name}' already exists.")
    return {"runner": await _runner_view(runner), "token": token}


@app.get("/runners")
async def list_runners() -> dict:
    active = await RunnerRepo(_p()).active_job_counts()
    now = _now()
    return {"runners": [_runner_json(r, active, now) for r in await RunnerRepo(_p()).list()]}


@app.patch("/runners/{runner_id}")
async def patch_runner(runner_id: str, req: RunnerPatchRequest) -> dict:
    runner = await RunnerRepo(_p()).update(runner_id, enabled=req.enabled)
    if runner is None:
        raise HTTPException(404, "runner not found")
    return await _runner_view(runner)


@app.delete("/runners/{runner_id}", status_code=204)
async def delete_runner(runner_id: str) -> Response:
    if not await RunnerRepo(_p()).delete(runner_id):
        raise HTTPException(404, "runner not found")
    return Response(status_code=204)


@app.get("/remote-jobs")
async def list_remote_jobs(swarm_id: Optional[str] = None) -> dict:
    """Remote jobs for the dashboard, long prompt bodies clipped; each carries the runner's
    and the task's name."""
    jobs = await RemoteJobRepo(_p()).list(swarm_id)
    runner_names = {r.id: r.name for r in await RunnerRepo(_p()).list()}
    task_ids = list({j.task_id for j in jobs})
    rows = await _p().fetch("SELECT id, name FROM tasks WHERE id = ANY($1::text[])", task_ids)
    task_names = {r["id"]: r["name"] for r in rows}
    out = []
    for j in jobs:
        d = R.clip_for_listing(_j(j))
        d["runner_name"] = runner_names.get(j.runner_id) if j.runner_id else None
        d["task_name"] = task_names.get(j.task_id)
        out.append(d)
    return {"jobs": out}


# -- runner protocol (docs/DESIGN-0.3.md section 5; runner bearer token) ------------------------------

#: Seconds between checks while a /runner/lease long-poll waits (tests shrink it).
LEASE_POLL_SECONDS = 1.0


class HelloRequest(BaseModel):
    host: str = ""
    version: str = ""
    max_jobs: int = Field(1, ge=1, le=64)
    harnesses: list[Any] = Field(default_factory=list)


class LeaseRequest(BaseModel):
    wait_seconds: float = Field(0, ge=0, le=R.MAX_WAIT_SECONDS)
    free_slots: int = Field(1, ge=0, le=64)


class HeartbeatRequest(BaseModel):
    note: Optional[str] = None


def _job_for_runner(job: RemoteJob) -> dict:
    """What the runner receives: the stored payload plus identity fields."""
    return {**job.payload, "id": job.id, "swarm_id": job.swarm_id, "task_id": job.task_id,
            "attempt": job.attempt, "harness": job.harness, "model": job.model,
            "workspace_url": f"/runner/jobs/{job.id}/workspace"}


@app.post("/runner/hello")
async def runner_hello(req: HelloRequest, runner: Runner = Depends(runner_auth)) -> dict:
    caps = R.normalize_capabilities(req.harnesses)
    await RunnerRepo(_p()).update(runner.id, host=req.host[:200], version=req.version[:80],
                                  max_jobs=req.max_jobs, harnesses=caps)
    return {"runner_id": runner.id, "name": runner.name, "poll_seconds": R.POLL_SECONDS,
            "heartbeat_seconds": R.heartbeat_seconds_for(settings.RUNNER_LEASE_SECONDS),
            "lease_seconds": settings.RUNNER_LEASE_SECONDS}


@app.post("/runner/lease")
async def runner_lease(req: LeaseRequest, runner: Runner = Depends(runner_auth)):
    """Long-poll for a matching queued job: 200 {"job"} or 204."""
    repo = RemoteJobRepo(_p())
    caps = R.capability_map(runner.harnesses)
    offered = R.harness_offered(runner.harnesses)
    deadline = time.monotonic() + req.wait_seconds
    while req.free_slots > 0:
        now = _now()
        await repo.requeue_expired(now)
        active = (await RunnerRepo(_p()).active_job_counts()).get(runner.id, 0)
        if active < runner.max_jobs:
            job = await repo.lease_next(runner, caps, now, settings.RUNNER_LEASE_SECONDS,
                                        harness_caps=offered)
            if job is not None:
                return {"job": _job_for_runner(job)}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(LEASE_POLL_SECONDS, remaining))
    return Response(status_code=204)


async def _own_job(job_id: str, runner: Runner) -> RemoteJob:
    """The job if it exists and this runner holds/held it, else 404."""
    job = await RemoteJobRepo(_p()).get(job_id)
    if job is None or job.runner_id != runner.id:
        raise HTTPException(404, "job not found for this runner")
    return job


@app.get("/runner/jobs/{job_id}/workspace")
async def runner_workspace(job_id: str, runner: Runner = Depends(runner_auth)) -> Response:
    job = await _own_job(job_id, runner)
    if job.status != "leased":
        raise HTTPException(404, "job is not leased by this runner")
    root = services.workspace_dir(settings, job.swarm_id)
    data = await asyncio.to_thread(R.build_workspace_tar, root)
    return Response(content=data, media_type="application/gzip")


async def _job_still_wanted(job: RemoteJob) -> bool:
    """False once the swarm left ACTIVE or the task is no longer In-Progress."""
    swarm = await SwarmRepo(_p()).get(job.swarm_id)
    if swarm is None or swarm.lifecycle_state != SwarmStatus.ACTIVE.value:
        return False
    task = await TaskRepo(_p()).get(job.task_id)
    return task is not None and task.status == "In-Progress"


@app.post("/runner/jobs/{job_id}/heartbeat")
async def runner_heartbeat(job_id: str, req: Optional[HeartbeatRequest] = None,
                           runner: Runner = Depends(runner_auth)) -> dict:
    repo = RemoteJobRepo(_p())
    job = await _own_job(job_id, runner)
    if job.status != "leased":
        return {"status": "cancelled"}
    if not await _job_still_wanted(job):
        await repo.cancel(job.id)
        return {"status": "cancelled"}
    beat = await repo.heartbeat(job.id, runner.id, _now(), settings.RUNNER_LEASE_SECONDS)
    return {"status": "leased" if beat is not None and beat.status == "leased" else "cancelled"}


@app.post("/runner/jobs/{job_id}/result")
async def runner_result(job_id: str, request: Request,
                        runner: Runner = Depends(runner_auth)) -> dict:
    repo = RemoteJobRepo(_p())
    job = await repo.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    raw = await request.body()
    if len(raw) > R.MAX_BODY_BYTES:
        raise HTTPException(413, "result body too large")
    if job.runner_id != runner.id or job.status != "leased":
        raise HTTPException(409, "job is not leased by this runner (cancelled or expired)")
    try:
        body = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "result is not valid JSON")
    limits = job.payload.get("limits") or {}
    try:
        result = R.validate_result(
            body, max_files=limits.get("max_files", R.MAX_FILES),
            max_file_bytes=limits.get("max_file_bytes", R.MAX_FILE_BYTES),
            max_total_bytes=limits.get("max_total_bytes", R.MAX_TOTAL_BYTES))
    except R.ResultError as exc:
        raise HTTPException(400, str(exc))
    root = services.ensure_workspace(settings, job.swarm_id)
    await asyncio.to_thread(R.stage_files, root, job.id, result.files)
    done = await repo.complete(job.id, runner.id, R.stored_result(result))
    if done is None:   # lost the lease while we validated
        await asyncio.to_thread(shutil.rmtree, R.staging_dir(root, job.id), True)
        raise HTTPException(409, "job is not leased by this runner (cancelled or expired)")
    return {"status": "completed"}
