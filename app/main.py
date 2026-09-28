from __future__ import annotations

import logging
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.config import Settings
from app.db import Database
from app.models import TaskCreate as TaskCreateModel, TaskUpdate as TaskUpdateModel
from app.repo import TaskRepository

settings = Settings()
db = Database(settings)
app = FastAPI(title="DAASH")
log = logging.getLogger(__name__)


@app.on_event("startup")
async def _startup() -> None:
    await db.connect()
    log.info("Database connection established.")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await db.disconnect()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# --- request models ---

class TaskCreateRequest(BaseModel):
    team_id: str
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    acceptance_criteria: str = ""


class TaskUpdateRequest(BaseModel):
    status: str | None = None
    description: str | None = None
    acceptance_criteria: str | None = None
    rejection_reason: str | None = None


# --- Task endpoints ---

@app.post("/tasks", status_code=201)
async def create_task(req: TaskCreateRequest) -> dict:
    repo = TaskRepository(db.pool)
    try:
        task = await repo.create(TaskCreateModel(**req.model_dump()))
    except Exception as e:
        if "team_id_fkey" in str(e):
            await db.pool.execute(
                "INSERT INTO teams (id, name, vision_statement, mission_statement) VALUES ($1, $1, 'Auto-created', '')",
                req.team_id,
            )
            task = await repo.create(TaskCreateModel(**req.model_dump()))
        else:
            raise
    return task.model_dump()


@app.get("/tasks")
async def list_tasks() -> dict:
    return {"tasks": []}


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
