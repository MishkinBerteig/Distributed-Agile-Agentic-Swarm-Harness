from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg
from app.models import Task, TaskCreate, TaskStatus, TaskUpdate


def _uid() -> str:
    return str(uuid.uuid4())


class TaskRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # -- create --

    async def create(self, data: TaskCreate) -> Task:
        kw_str = " ".join(data.keywords) if data.keywords else None

        if data.parent_id:
            sql = (
                "INSERT INTO tasks (id, team_id, parent_id, name, description, "
                "acceptance_criteria, status, keywords) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'Pending', $7) "
                "RETURNING id, team_id, parent_id, name, description, "
                "acceptance_criteria, status, rejection_reason, created_at, updated_at, keywords"
            )
            row = await self._pool.fetchrow(
                sql, _uid(), data.team_id, data.parent_id,
                data.name, data.description, data.acceptance_criteria, kw_str,
            )
        else:
            sql = (
                "INSERT INTO tasks (id, team_id, parent_id, name, description, "
                "acceptance_criteria, status, keywords) "
                "VALUES ($1, $2, NULL, $3, $4, $5, 'Pending', $6) "
                "RETURNING id, team_id, parent_id, name, description, "
                "acceptance_criteria, status, rejection_reason, created_at, updated_at, keywords"
            )
            row = await self._pool.fetchrow(
                sql, _uid(), data.team_id, data.name,
                data.description, data.acceptance_criteria, kw_str,
            )
        return self._row_to_task(row)

    # -- read --

    async def get(self, task_id: str) -> Task | None:
        row = await self._pool.fetchrow(
            "SELECT * FROM tasks WHERE id = $1", task_id
        )
        if row is None:
            return None
        return self._row_to_task(row)

    async def list_by_team(self, team_id: str, status: TaskStatus | None = None) -> list[Task]:
        query = "SELECT * FROM tasks WHERE team_id = $1"
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
        }

        for attr, col in update_map.items():
            val = getattr(data, attr, None)
            if val is not None:
                fields.append(f"{col} = ${idx}")
                params.append(val)
                idx += 1

        if not fields:
            existing = await self.get(task_id)
            if existing is None:
                return None
            return existing

        fields.append("updated_at = now()")
        row = await self._pool.fetchrow(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = $1 RETURNING *",
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
