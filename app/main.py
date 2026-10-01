from __future__ import annotations

import asyncio
import logging
import threading

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.bus import DecisionBus, ProposalError
from app.config import Settings
from app.db import Database
from app.embeddings import build_embedder
from app.models import TaskCreate as TaskCreateModel, TaskUpdate as TaskUpdateModel
from app.models import TaskStatus
from app.repo import TaskRepository, TeamRepository

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    await db.connect()
    log.info("Database connection established.")


@app.on_event("shutdown")
async def _shutdown() -> None:
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


class SwarmCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    vision_statement: str = ""
    mission_statement: str = ""


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


# --- Swarm / Team endpoints ---

@app.get("/teams")
async def list_teams() -> dict:
    repo = TeamRepository(db.pool)
    teams = await repo.list()
    return {"teams": [t.model_dump(mode="json") for t in teams]}


@app.post("/swarms", status_code=201)
async def create_swarm(req: SwarmCreateRequest) -> dict:
    repo = TeamRepository(db.pool)
    team = await repo.create(req.name, req.vision_statement, req.mission_statement)
    return team.model_dump(mode="json")


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
                "INSERT INTO teams (id, name, vision_statement, mission_statement) VALUES ($1, $1, 'Auto-created', '') ON CONFLICT DO NOTHING",
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
