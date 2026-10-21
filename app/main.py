from __future__ import annotations

import asyncio
import logging
import threading

import asyncpg
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.adapter import (
    AgentMessage,
    AgentTurn,
    HarnessAdapterError,
    build_adapter,
    get_active_adapter,
)
from app.bus import DecisionBus, ProposalError
from app.config import Settings
from app.db import Database
from app.embeddings import build_embedder
from app.models import SwarmCreateRequest, TeamCreate as TeamCreateRequest, TeamCreateItem
from app.models import TaskCreate as TaskCreateModel, TaskUpdate as TaskUpdateModel
from app.models import TaskStatus, SwarmStatus
from app.prompts import composeMissionPrompt, composeQualityPrompt, extract_quality_json
from app.repo import AgentSessionRepository, SwarmRepository, TaskRepository, TeamRepository
from app.sessions import RunnerClient, SessionOrchestrator

settings = Settings()
db = Database(settings)
# Decision bus (Slice 2): ephemeral Redis signals + quorum voting. Holds no
# durable state — PostgreSQL remains the source of record.
bus = DecisionBus(
    settings.REDIS_URL,
    ttl_seconds=settings.BUS_TTL_SECONDS,
    signals_max=settings.BUS_SIGNALS_MAX,
)
app = FastAPI(title="DAASH")
log = logging.getLogger(__name__)

# Primary embedding model: Alibaba-NLP/gte-base-en-v1.5 (768-dim). Resolved
# lazily on first use; falls back to a deterministic embedder when the model
# or sentence-transformers is unavailable so task creation never blocks.
embedder = build_embedder(settings.EMBEDDING_MODEL, settings.EMBEDDING_USE_MODEL)

# Harness adapter (Slice 5): the composite adapter that Quality Judgement runs
# through. Building it only constructs objects (no I/O), so we do it at import
# time — ASGI lifespan events don't fire under ASGITransport in tests, and the
# adapter must be ready before the first /teams/{id}/quality request either way.
adapter = build_adapter(settings)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session orchestrator (Slice 5.1). Built lazily against db.pool because tests
# swap main.db for the test database after import — same reason the adapter is
# built at module scope but consumed per request. When RUNNER_URL is empty the
# client stays disabled and every session call degrades to a no-op, which keeps
# the suite (and any host-less dev setup) green without a runner.
_orch: SessionOrchestrator | None = None


def _orchestrator() -> SessionOrchestrator:
    global _orch
    if _orch is None or _orch._pool is not db.pool:
        _orch = SessionOrchestrator(db.pool, RunnerClient(settings), settings)
    return _orch


async def _stop_sessions(swarm_id: str) -> None:
    """Best-effort terminate; errors are logged inside the orchestrator."""
    await _orchestrator().stop_swarm(swarm_id)


@app.on_event("startup")
async def _startup() -> None:
    await db.connect()
    log.info("Database connection established.")
    if settings.RUNNER_URL:
        task = asyncio.create_task(_orchestrator().run_loop())
        app.state.reconcile_task = task
        log.info("Session reconcile loop started (every %ss).", settings.SESSION_RECONCILE_SECONDS)


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "reconcile_task", None)
    if task is not None:
        task.cancel()
    await db.disconnect()
    await bus.close()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# --- request models ---

class TaskCreateRequest(BaseModel):
    team_id: str
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    acceptance_criteria: str = ""
    parent_id: str | None = None


class TaskUpdateRequest(BaseModel):
    status: TaskStatus | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    rejection_reason: str | None = None


class SignalPublishRequest(BaseModel):
    kind: str = Field(..., min_length=1, max_length=100)
    team_id: str | None = None
    task_id: str | None = None
    payload: dict = Field(default_factory=dict)


class ProposalCreateRequest(BaseModel):
    team_id: str = Field(..., min_length=1)
    question: str = Field(..., min_length=1)
    quorum: int = Field(1, ge=1)
    task_id: str | None = None


class VoteRequest(BaseModel):
    voter: str = Field(..., min_length=1)
    approve: bool


class MissionJudgementRequest(BaseModel):
    """Payload for the Mission Judgement endpoint."""
    team_id: str
    task_text: str


class QualityGenerateRequest(BaseModel):
    """Optional user guidance for the Quality Judgement generation prompt."""
    dor_guidance: str = ""
    dod_guidance: str = ""


# --- Swarm / Team endpoints ---
#
# Slice 5.1 restructure: a swarm owns the Vision and the lifecycle; teams (its
# members' org units) own Missions and carry no lifecycle of their own yet.

@app.get("/swarms")
async def list_swarms() -> dict:
    repo = SwarmRepository(db.pool)
    swarms = await repo.list()
    return {"swarms": [s.model_dump(mode="json") for s in swarms]}


@app.get("/swarms/active")
async def get_active_swarm():
    repo = SwarmRepository(db.pool)
    swarm = await repo.get_active()
    if swarm is None:
        return None
    return swarm.model_dump(mode="json")


@app.post("/swarms", status_code=201)
async def create_swarm(req: SwarmCreateRequest) -> dict:
    """Create a swarm (vision + lifecycle home) and seed it with ≥1 team."""
    repo = SwarmRepository(db.pool)
    # Enforce: at most one live (non-ARCHIVED) swarm at any time.
    existing = await repo.get_active()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Swarm {existing.id} is already live ({existing.lifecycle_state}). "
                   f"Archive or delete it before creating a new one.",
        )
    swarm = await repo.create(req.name, req.vision_statement)
    team_repo = TeamRepository(db.pool)
    items = req.teams or [TeamCreateItem(name=req.name, mission_statement=req.mission_statement)]
    teams = [await team_repo.create(swarm.id, t.name, t.mission_statement) for t in items]
    out = swarm.model_dump(mode="json")
    out["teams"] = [t.model_dump(mode="json") for t in teams]
    return out


@app.get("/swarms/{swarm_id}")
async def get_swarm(swarm_id: str) -> dict:
    repo = SwarmRepository(db.pool)
    swarm = await repo.get(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")
    teams = await TeamRepository(db.pool).list(swarm_id=swarm_id)
    sessions = await AgentSessionRepository(db.pool).list_for_swarm(swarm_id)
    out = swarm.model_dump(mode="json")
    out["teams"] = [t.model_dump(mode="json") for t in teams]
    out["sessions"] = [s.model_dump(mode="json") for s in sessions]
    return out


@app.get("/swarms/{swarm_id}/sessions")
async def list_swarm_sessions(swarm_id: str) -> dict:
    """Live roster: per team, the three member sessions with harness/model/state."""
    repo = SwarmRepository(db.pool)
    swarm = await repo.get(swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")
    try:
        await _orchestrator().reconcile_swarm(swarm_id)  # refresh against the runner
    except Exception as exc:  # noqa: BLE001 — roster must render even if runner is dead
        log.debug("session reconcile failed for %s: %r", swarm_id, exc)
    sessions = await AgentSessionRepository(db.pool).list_for_swarm(swarm_id)
    teams = {t.id: t.model_dump(mode="json") for t in await TeamRepository(db.pool).list(swarm_id=swarm_id)}
    roster: dict[str, list] = {}
    for s in sessions:
        entry = s.model_dump(mode="json")
        team = teams.get(s.team_id)
        entry["team_name"] = team["name"] if team else s.team_id
        roster.setdefault(s.team_id, []).append(entry)
    return {"swarm_id": swarm_id, "teams": [{"team_id": k, "sessions": v} for k, v in roster.items()]}


@app.post("/swarms/{swarm_id}/sessions/{session_id}/turns", status_code=202)
async def send_session_turn(swarm_id: str, session_id: str, body: dict = Body(...)) -> dict:
    sess_repo = AgentSessionRepository(db.pool)
    sess = await sess_repo.get(session_id)
    if sess is None or sess.swarm_id != swarm_id:
        raise HTTPException(status_code=404, detail="Session not found")
    if not await _orchestrator()._client.send_turn(session_id, str(body.get("text", ""))):
        raise HTTPException(status_code=409, detail="Session is not reachable on the runner")
    return {"accepted": True}


@app.delete("/swarms/{swarm_id}/delete")
async def delete_swarm(swarm_id: str) -> dict:
    repo = SwarmRepository(db.pool)
    try:
        await _stop_sessions(swarm_id)
        await repo.hard_delete(swarm_id)
        return {"deleted": swarm_id}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# Actions that must not leave agent sessions running.
_STOP_ACTIONS = {"pause", "feedback", "verify", "learn", "archive", "delete"}


@app.post("/swarms/{swarm_id}/transitions/{action}")
async def transition_swarm(swarm_id: str, action: str) -> dict:
    repo = SwarmRepository(db.pool)
    try:
        swarm = await repo.transition(swarm_id, action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Slice 5.1: going live launches each member's harness session; leaving the
    # field terminates them. Launch/stop failures never fail the transition —
    # they surface on the roster as failed sessions.
    try:
        if action in ("start", "resume"):
            await _orchestrator().launch_swarm(swarm)
        elif action in _STOP_ACTIONS:
            await _stop_sessions(swarm_id)
    except Exception as exc:  # noqa: BLE001 — session errors must not undo a legal transition
        log.warning("session side effect for %s(%s) failed: %r", action, swarm_id, exc)

    return swarm.model_dump(mode="json")


@app.get("/teams")
async def list_teams(swarm_id: str | None = None) -> dict:
    repo = TeamRepository(db.pool)
    teams = await repo.list(swarm_id=swarm_id)
    return {"teams": [t.model_dump(mode="json") for t in teams]}


@app.post("/teams", status_code=201)
async def create_team(req: TeamCreateRequest) -> dict:
    swarm = await SwarmRepository(db.pool).get(req.swarm_id)
    if swarm is None:
        raise HTTPException(status_code=404, detail="Swarm not found")
    team = await TeamRepository(db.pool).create(req.swarm_id, req.name, req.mission_statement)
    return team.model_dump(mode="json")


@app.get("/teams/{team_id}")
async def get_team(team_id: str) -> dict:
    repo = TeamRepository(db.pool)
    team = await repo.get(team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    return team.model_dump(mode="json")


@app.delete("/teams/{team_id}")
async def delete_team(team_id: str) -> dict:
    try:
        await TeamRepository(db.pool).delete(team_id)
        return {"deleted": team_id}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# --- Quality Judgement endpoints (Slice 5.0) ---

@app.post("/teams/{team_id}/quality", status_code=201)
async def generate_team_quality(team_id: str, req: QualityGenerateRequest | None = Body(default=None)) -> dict:
    """Quality Judgement: derive the team's Definition of Ready / Done via the harness.

    Runs the LMStudio harness adapter with a prompt built from the team's
    Vision/Mission plus optional user guidance, parses strict JSON back out, and
    persists both standards on the team row (regenerating overwrites cleanly).
    """
    repo = TeamRepository(db.pool)
    team = await repo.get(team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")

    # The deterministic hierarchy (README): swarm Vision flows into every team.
    swarm = await SwarmRepository(db.pool).get(team.swarm_id)

    system_prompt = composeQualityPrompt(
        team_vision=(swarm.vision_statement if swarm else team.vision_statement) or "",
        team_mission=team.mission_statement or "",
        dor_guidance=(req.dor_guidance if req else "") or None,
        dod_guidance=(req.dod_guidance if req else "") or None,
    )
    turn = AgentTurn(
        system_prompt=system_prompt,
        messages=[AgentMessage(role="user", content="Generate the team quality standards now.")],
        temperature=0.2,
        metadata={"role": "quality_judge", "team_id": team.id},
    )

    try:
        response = await get_active_adapter().execute(turn)
    except HarnessAdapterError as exc:
        status = 409 if exc.code == "busy" else 502
        raise HTTPException(status, f"Harness error during quality generation: {exc.message}")
    except Exception as exc:  # transport/timeout errors surface here too
        log.warning("Quality generation failed for team %s: %r", team_id, exc)
        raise HTTPException(502, "Harness adapter unreachable — is LMStudio running?")

    try:
        standards = extract_quality_json(response.text)
    except ValueError as exc:
        log.warning("Unparseable quality JSON from harness (team %s): %r", team_id, response.text[:400])
        raise HTTPException(502, f"Harness returned unparseable quality JSON: {exc}")

    persisted = await repo.update_quality_standards(
        team.id, standards["definition_of_ready"], standards["definition_of_done"]
    )
    if persisted is None:  # team vanished between get and update
        raise HTTPException(status_code=404, detail="Team not found")

    # Best-effort decision-bus signal so the swarm sees quality evolution.
    try:
        await bus.publish(
            "quality.generated",
            team_id=team.id,
            payload={"definition_of_ready": standards["definition_of_ready"][:200],
                     "definition_of_done": standards["definition_of_done"][:200]},
        )
    except Exception as exc:
        log.warning("Failed to publish quality.generated signal: %r", exc)

    return {
        **persisted,
        "model": response.model,
        "usage": response.usage,
    }


@app.get("/teams/{team_id}/quality")
async def get_team_quality(team_id: str) -> dict:
    """Return the team's stored Definition of Ready / Done (None until generated)."""
    repo = TeamRepository(db.pool)
    standards = await repo.get_quality_standards(team_id)
    if standards is None:
        raise HTTPException(status_code=404, detail="Swarm not found")
    return standards


# --- Task endpoints ---

@app.post("/tasks", status_code=201)
async def create_task(req: TaskCreateRequest) -> dict:
    repo = TaskRepository(db.pool)
    if req.parent_id and not await db.pool.fetchval(
        "SELECT 1 FROM tasks WHERE id = $1", req.parent_id,
    ):
        raise HTTPException(422, f"parent task {req.parent_id} not found")

    embedding = await asyncio.to_thread(
        embedder.embed,
        "\n".join(filter(None, [req.name, req.description, req.acceptance_criteria])),
    )

    try:
        task = await repo.create(TaskCreateModel(**req.model_dump()), embedding=embedding)
    except asyncpg.ForeignKeyViolationError as e:
        if "team_id_fkey" in str(e):
            await db.pool.execute(
                "INSERT INTO teams (id, name, vision_statement, mission_statement, lifecycle_state) VALUES ($1, $1, 'Auto-created', '', 'CREATED') ON CONFLICT (id) DO NOTHING",
                req.team_id,
            )
            try:
                task = await repo.create(TaskCreateModel(**req.model_dump()), embedding=embedding)
            except asyncpg.ForeignKeyViolationError as retry_e:
                if "parent_id_fkey" in str(retry_e):
                    raise HTTPException(422, f"parent task {req.parent_id} not found") from retry_e
                raise
        else:
            raise HTTPException(422, f"parent task {req.parent_id} not found")
    return task.model_dump()


# --- alignment helpers ---

ALIGNMENT_THRESHOLD: float = 0.7


def _compute_alignment_score(task_text: str, mission_text: str) -> float:
    """Cosine similarity between task and mission embeddings.

    Returns a value in [0, 1].  Zero means no overlap; 1 means identical direction.
    """
    task_vec = embedder.embed(task_text)
    mission_vec = embedder.embed(mission_text)

    # Cosine similarity
    dot = sum(a * b for a, b in zip(task_vec, mission_vec))
    t_norm = sum(a * a for a in task_vec) ** 0.5
    m_norm = sum(a * a for a in mission_vec) ** 0.5
    if t_norm == 0.0 or m_norm == 0.0:
        return 0.0
    return dot / (t_norm * m_norm)


@app.patch("/tasks/{task_id}/judgement")
async def judgement_task(task_id: str, req: MissionJudgementRequest = Body(...)) -> dict:
    """Mission Judgement: check if a task aligns with the team's Vision/Mission."""
    repo = TeamRepository(db.pool)
    team = await repo.get_by_id(req.team_id)
    if not team:
        raise HTTPException(404, "team not found")

    # Build the mission text the agent will use
    mission_text = f"{team.vision_statement}\n\n{team.mission_statement}"

    # Compute alignment score via embedding cosine similarity
    score = _compute_alignment_score(req.task_text, mission_text)

    aligned = score >= ALIGNMENT_THRESHOLD

    # Compose the Mission prompt for this context
    system_prompt = composeMissionPrompt(
        team_vision=team.vision_statement,
        team_mission=team.mission_statement,
        task_name=req.task_text,
    )

    return {
        "task_id": task_id,
        "team_id": req.team_id,
        "aligned": aligned,
        "score": round(score, 4),
        "reason": "Aligned with mission" if aligned else "Below alignment threshold",
        "system_prompt": system_prompt,
    }


@app.get("/tasks")
async def list_tasks(team_id: str, status: TaskStatus | None = None) -> dict:
    repo = TaskRepository(db.pool)
    tasks = await repo.list_by_team(team_id, status)
    return {"tasks": [t.model_dump() for t in tasks]}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    repo = TaskRepository(db.pool)
    task = await repo.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task.model_dump()


@app.patch("/tasks/{task_id}")
async def update_task(task_id: str, req: TaskUpdateRequest) -> dict:
    repo = TaskRepository(db.pool)
    task = await repo.update(task_id, TaskUpdateModel(**req.model_dump(exclude_none=True)))
    if task is None:
        raise HTTPException(404, "task not found")
    return task.model_dump()


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str) -> None:
    repo = TaskRepository(db.pool)
    ok = await repo.delete(task_id)
    if not ok:
        raise HTTPException(404, "task not found")


# --- Decision Bus endpoints (Slice 2) ---

@app.post("/decisions", status_code=201)
async def publish_decision(req: SignalPublishRequest) -> dict:
    signal = await bus.publish(
        req.kind, team_id=req.team_id, task_id=req.task_id, payload=req.payload
    )
    return signal.model_dump()


@app.get("/decisions")
async def list_decisions(limit: int = 50) -> dict:
    signals = await bus.recent_signals(limit=limit)
    return {"signals": [s.model_dump() for s in signals]}


@app.post("/proposals", status_code=201)
async def create_proposal(req: ProposalCreateRequest) -> dict:
    proposal = await bus.create_proposal(
        req.team_id, req.question, quorum=req.quorum, task_id=req.task_id
    )
    return proposal.model_dump()


@app.get("/proposals")
async def list_proposals(team_id: str | None = None) -> dict:
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
    await bus.publish(
        signal_kind,
        team_id=proposal.team_id,
        task_id=proposal.task_id,
        payload={
            "proposal_id": proposal.id,
            "voter": req.voter,
            "vote": "approve" if req.approve else "reject",
            "status": proposal.status,
        },
    )
    return proposal.model_dump()
