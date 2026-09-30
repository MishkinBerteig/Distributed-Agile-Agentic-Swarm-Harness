from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg
from app.models import Task, TaskCreate, TaskStatus, TaskUpdate, Team


def _uid() -> str:
    return str(uuid.uuid4())


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class TaskRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # -- create --

    async def create(self, data: TaskCreate, embedding: list[float] | None = None) -> Task:
        kw_str = " ".join(data.keywords) if data.keywords else None
        vec = _vec_literal(embedding) if embedding is not None else None

        if data.parent_id:
            sql = (
                "INSERT INTO tasks (id, team_id, parent_id, name, description, "
                "acceptance_criteria, status, keywords, embedding) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'Pending', $7, $8::vector) "
                "RETURNING id, team_id, parent_id, name, description, "
                "acceptance_criteria, status, rejection_reason, created_at, updated_at, keywords"
            )
            row = await self._pool.fetchrow(
                sql, _uid(), data.team_id, data.parent_id,
                data.name, data.description, data.acceptance_criteria, kw_str, vec,
            )
        else:
            sql = (
                "INSERT INTO tasks (id, team_id, parent_id, name, description, "
                "acceptance_criteria, status, keywords, embedding) "
                "VALUES ($1, $2, NULL, $3, $4, $5, 'Pending', $6, $7::vector) "
                "RETURNING id, team_id, parent_id, name, description, "
                "acceptance_criteria, status, rejection_reason, created_at, updated_at, keywords"
            )
            row = await self._pool.fetchrow(
                sql, _uid(), data.team_id, data.name,
                data.description, data.acceptance_criteria, kw_str, vec,
            )
        return self._row_to_task(row)

    # -- read --

    @staticmethod
    def _task_cols() -> str:
        # Explicit column list: keeps pgvector `embedding` out of reads (asyncpg
        # has no codec registered for it and the API doesn't expose vectors).
        return (
            "id, team_id, parent_id, name, description, acceptance_criteria, "
            "status, rejection_reason, created_at, updated_at, keywords"
        )

    async def get(self, task_id: str) -> Task | None:
        row = await self._pool.fetchrow(
            f"SELECT {self._task_cols()} FROM tasks WHERE id = $1", task_id
        )
        if row is None:
            return None
        return self._row_to_task(row)

    async def list_by_team(self, team_id: str, status: TaskStatus | None = None) -> list[Task]:
        query = f"SELECT {self._task_cols()} FROM tasks WHERE team_id = $1"
        params: list = [team_id]
        idx = 2
        if status is not None:
            query += f" AND status = ${idx}"
            params.append(status.value)
            idx += 1
        query += " ORDER BY created_at"
        rows = await self._pool.fetch(query, *params)
        return [self._row_to_task(r) for r in rows]

    # -- update --

    async def update(self, task_id: str, data: TaskUpdate) -> Task | None:
        fields: list[str] = []
        params: list = [task_id]
        idx = 2

        # Map TaskUpdate fields to DB columns
        update_map = {
            "name": "name",
            "description": "description",
            "status": "status",
            "rejection_reason": "rejection_reason",
            "acceptance_criteria": "acceptance_criteria",
        }

        for attr, col in update_map.items():
            val = getattr(data, attr, None)
            if val is not None:
                fields.append(f"{col} = ${idx}")
                params.append(val.value if isinstance(val, TaskStatus) else val)
                idx += 1

        if not fields:
            existing = await self.get(task_id)
            if existing is None:
                return None
            return existing

        fields.append("updated_at = now()")
        row = await self._pool.fetchrow(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = $1 "
            f"RETURNING {self._task_cols()}",
            *params,
        )
        if row is None:
            return None
        return self._row_to_task(row)

    # -- delete --

    async def delete(self, task_id: str) -> bool:
        result = await self._pool.execute("DELETE FROM tasks WHERE id = $1", task_id)
        return result == "DELETE 1"

    # -- helpers --

    @staticmethod
    def _row_to_task(row: asyncpg.Record) -> Task:
        kw = row.get("keywords")
        keywords = [k for k in kw.split() if k] if kw else []
        return Task(
            id=row["id"],
            team_id=row["team_id"],
            parent_id=row.get("parent_id"),
            name=row["name"],
            description=row["description"],
            acceptance_criteria=row["acceptance_criteria"],
            status=TaskStatus(row["status"]),
            keywords=keywords,
            rejection_reason=row.get("rejection_reason"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class TeamRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def create(self, name: str, vision_statement: str, mission_statement: str = "") -> Team:
        row = await self._pool.fetchrow(
            "INSERT INTO teams (id, name, vision_statement, mission_statement) "
            "VALUES ($1, $2, $3, $4) RETURNING id, name, vision_statement, mission_statement, created_at",
            _uid(), name, vision_statement, mission_statement,
        )
        return self._row_to_team(row)

    async def list(self) -> list[Team]:
        rows = await self._pool.fetch(
            "SELECT id, name, vision_statement, mission_statement, created_at "
            "FROM teams ORDER BY created_at"
        )
        return [self._row_to_team(r) for r in rows]

    async def get(self, team_id: str) -> Team | None:
        row = await self._pool.fetchrow(
            "SELECT id, name, vision_statement, mission_statement, created_at FROM teams WHERE id = $1",
            team_id,
        )
        if row is None:
            return None
        return self._row_to_team(row)

    @staticmethod
    def _row_to_team(row: asyncpg.Record) -> Team:
        return Team(
            id=row["id"],
            name=row["name"],
            vision_statement=row["vision_statement"],
            mission_statement=row["mission_statement"],
            created_at=row["created_at"],
        )
