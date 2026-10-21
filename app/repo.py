from __future__ import annotations

import uuid
from datetime import datetime

import asyncpg
from app.models import (AgentSession, Swarm, SwarmStatus, Task, TaskCreate,
                        TaskStatus, TaskUpdate, Team)


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
                return existing
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



class SwarmRepository:
    """Swarm-level state: vision + lifecycle (Slice 5.1 restructure)."""

    #: Action verb -> {from_state: to_state}. Same rules that lived on teams
    # since Slice 3; the lifecycle simply moved up to the swarm row.
    ACTION_MAP = {
        "start":    {"CREATED": "ACTIVE"},
        "pause":    {"ACTIVE": "PAUSED", "USER_FEEDBACK": "PAUSED"},
        "resume":   {"PAUSED": "ACTIVE", "USER_FEEDBACK": "ACTIVE"},
        "feedback": {"PAUSED": "USER_FEEDBACK"},
        "verify":   {"ACTIVE": "VERIFICATION", "PAUSED": "VERIFICATION"},
        "learn":    {"VERIFICATION": "LEARNING"},
        "archive":  {"VERIFICATION": "ARCHIVED", "LEARNING": "ARCHIVED"},
        "delete":   {"CREATED": "DELETED", "VERIFICATION": "DELETED"},
    }

    _cols = "id, name, vision_statement, lifecycle_state, created_at"

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def create(self, name: str, vision_statement: str) -> Swarm:
        row = await self._pool.fetchrow(
            f"INSERT INTO swarms (id, name, vision_statement) VALUES ($1, $2, $3) RETURNING {self._cols}",
            _uid(), name, vision_statement,
        )
        return self._row_to_swarm(row)

    async def list(self) -> list[Swarm]:
        rows = await self._pool.fetch(f"SELECT {self._cols} FROM swarms ORDER BY created_at")
        return [self._row_to_swarm(r) for r in rows]

    async def get(self, swarm_id: str) -> Swarm | None:
        row = await self._pool.fetchrow(
            f"SELECT {self._cols} FROM swarms WHERE id = $1", swarm_id)
        return self._row_to_swarm(row) if row else None

    async def get_active(self) -> Swarm | None:
        """The single live (non-terminal) swarm, or None. One-at-a-time invariant."""
        terminal = SwarmStatus.terminal()
        placeholders = ",".join(f"'{s}'" for s in terminal)
        row = await self._pool.fetchrow(
            f"SELECT {self._cols} FROM swarms WHERE lifecycle_state NOT IN ({placeholders}) "
            "ORDER BY created_at LIMIT 1")
        return self._row_to_swarm(row) if row else None

    async def transition(self, swarm_id: str, action: str) -> Swarm:
        """Apply a lifecycle action verb. Raises ValueError when disallowed."""
        row = await self._pool.fetchrow(
            "SELECT lifecycle_state FROM swarms WHERE id = $1", swarm_id)
        if row is None:
            raise ValueError(f"Swarm {swarm_id} not found")
        current = row["lifecycle_state"]
        allowed = self.ACTION_MAP.get(action, {})
        if current not in allowed:
            raise ValueError(
                f"Cannot {action} from state {current}: allowed from {sorted(allowed)}")
        target = allowed[current]
        updated = await self._pool.fetchrow(
            f"UPDATE swarms SET lifecycle_state = $2 WHERE id = $1 RETURNING {self._cols}",
            swarm_id, target)
        return self._row_to_swarm(updated)

    async def hard_delete(self, swarm_id: str) -> None:
        """Delete a swarm from any live state; cascades its teams and tasks."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                teams = await conn.fetch("SELECT id FROM teams WHERE swarm_id = $1", swarm_id)
                for team in teams:
                    tid = team["id"]
                    await conn.execute(
                        "DELETE FROM memory_entries WHERE task_id IN (SELECT id FROM tasks WHERE team_id = $1)", tid)
                    await conn.execute("DELETE FROM transcripts WHERE team_id = $1", tid)
                    await conn.execute("DELETE FROM agent_sessions WHERE team_id = $1", tid)
                    await conn.execute("DELETE FROM tasks WHERE team_id = $1", tid)
                await conn.execute("DELETE FROM teams WHERE swarm_id = $1", swarm_id)
                result = await conn.execute(
                    "DELETE FROM swarms WHERE id = $1", swarm_id)
                if result != "DELETE 1":
                    raise ValueError(f"Swarm {swarm_id} not found for deletion")

    @staticmethod
    def _row_to_swarm(row: asyncpg.Record) -> Swarm:
        return Swarm(
            id=row["id"], name=row["name"], vision_statement=row["vision_statement"],
            lifecycle_state=row["lifecycle_state"], created_at=row["created_at"])


class TeamRepository:
    """Team-level state: mission + quality standards. No lifecycle (yet)."""

    _cols = ("id, swarm_id, name, vision_statement, mission_statement, "
             "lifecycle_state, created_at")

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def create(self, swarm_id: str, name: str, mission_statement: str = "") -> Team:
        row = await self._pool.fetchrow(
            f"INSERT INTO teams (id, swarm_id, name, mission_statement) "
            f"VALUES ($1, $2, $3, $4) RETURNING {self._cols}",
            _uid(), swarm_id, name, mission_statement)
        return self._row_to_team(row)

    async def list(self, swarm_id: str | None = None) -> list[Team]:
        if swarm_id is not None:
            rows = await self._pool.fetch(
                f"SELECT {self._cols} FROM teams WHERE swarm_id = $1 ORDER BY created_at", swarm_id)
        else:
            rows = await self._pool.fetch(f"SELECT {self._cols} FROM teams ORDER BY created_at")
        return [self._row_to_team(r) for r in rows]

    async def get(self, team_id: str) -> Team | None:
        row = await self._pool.fetchrow(
            f"SELECT {self._cols} FROM teams WHERE id = $1", team_id)
        return self._row_to_team(row) if row else None

    # Convenience alias kept for existing call sites.
    get_by_id = get

    async def upsert(self, data: dict) -> Team:
        """Insert or update a team. Returns the upserted row.

        Every field except ``id`` is optional, so tests and tooling can seed
        standalone teams (no swarm) exactly as before the 5.1 restructure.
        """
        row = await self._pool.fetchrow(
            "INSERT INTO teams (id, swarm_id, name, vision_statement, mission_statement, lifecycle_state) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (id) DO UPDATE SET "
            "    swarm_id = COALESCE(EXCLUDED.swarm_id, teams.swarm_id), "
            "    name = EXCLUDED.name, "
            "    vision_statement = COALESCE(EXCLUDED.vision_statement, teams.vision_statement), "
            "    mission_statement = COALESCE(EXCLUDED.mission_statement, teams.mission_statement), "
            "    lifecycle_state = COALESCE(EXCLUDED.lifecycle_state, teams.lifecycle_state) "
            f"RETURNING {self._cols}",
            data["id"], data.get("swarm_id"), data["name"],
            data.get("vision_statement"), data.get("mission_statement"),
            data.get("lifecycle_state"))
        return self._row_to_team(row)

    async def delete(self, team_id: str) -> None:
        """Delete a single team (and its tasks). Raises ValueError if absent."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM memory_entries WHERE task_id IN (SELECT id FROM tasks WHERE team_id = $1)",
                    team_id)
                await conn.execute("DELETE FROM transcripts WHERE team_id = $1", team_id)
                await conn.execute("DELETE FROM agent_sessions WHERE team_id = $1", team_id)
                result = await conn.execute("DELETE FROM teams WHERE id = $1", team_id)
                if result != "DELETE 1":
                    raise ValueError(f"Team {team_id} not found for deletion")

    async def get_quality_standards(self, team_id: str) -> dict | None:
        """Return the team's Definition of Ready/Done, or None if no such team."""
        row = await self._pool.fetchrow(
            "SELECT definition_of_ready, definition_of_done FROM teams WHERE id = $1", team_id)
        if row is None:
            return None
        return {"team_id": team_id,
                "definition_of_ready": row["definition_of_ready"],
                "definition_of_done": row["definition_of_done"]}

    async def update_quality_standards(self, team_id: str, definition_of_ready: str,
                                       definition_of_done: str) -> dict | None:
        """Persist generated DoR/DoD for a team. Returns None if no such team."""
        row = await self._pool.fetchrow(
            "UPDATE teams SET definition_of_ready = $2, definition_of_done = $3 WHERE id = $1 "
            "RETURNING definition_of_ready, definition_of_done",
            team_id, definition_of_ready, definition_of_done)
        if row is None:
            return None
        return {"team_id": team_id,
                "definition_of_ready": row["definition_of_ready"],
                "definition_of_done": row["definition_of_done"]}

    @staticmethod
    def _row_to_team(row: asyncpg.Record) -> Team:
        return Team(
            id=row["id"], swarm_id=row["swarm_id"], name=row["name"],
            vision_statement=row["vision_statement"],
            mission_statement=row["mission_statement"] or "",
            lifecycle_state=row["lifecycle_state"], created_at=row["created_at"])


class AgentSessionRepository:
    """One row per launched harness session (Slice 5.1)."""

    _cols = ("id, swarm_id, team_id, role, harness, model, session_state, error, "
             "harness_session_id, turns, launched_at, last_seen")

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def create(self, *, id: str, swarm_id: str, team_id: str, role: str,
                     harness: str = "openclaude", session_state: str = "pending") -> AgentSession:
        row = await self._pool.fetchrow(
            f"INSERT INTO agent_sessions (id, swarm_id, team_id, role, harness, session_state) "
            f"VALUES ($1, $2, $3, $4, $5, $6) RETURNING {self._cols}",
            id, swarm_id, team_id, role, harness, session_state)
        return self._row(row)

    async def list_for_swarm(self, swarm_id: str) -> list[AgentSession]:
        rows = await self._pool.fetch(
            f"SELECT {self._cols} FROM agent_sessions WHERE swarm_id = $1 "
            "ORDER BY team_id, role", swarm_id)
        return [self._row(r) for r in rows]

    async def get(self, session_id: str) -> AgentSession | None:
        row = await self._pool.fetchrow(
            f"SELECT {self._cols} FROM agent_sessions WHERE id = $1", session_id)
        return self._row(row) if row else None

    async def update_state(self, session_id: str, state: str, *, error: str | None = None,
                           model: str | None = None, turns: int | None = None,
                           harness_session_id: str | None = None) -> AgentSession | None:
        row = await self._pool.fetchrow(
            "UPDATE agent_sessions SET session_state = $2, error = COALESCE($3, error), "
            "model = COALESCE($4, model), turns = COALESCE($5, turns), "
            "harness_session_id = COALESCE($6, harness_session_id), last_seen = now() "
            f"WHERE id = $1 RETURNING {self._cols}",
            session_id, state, error, model, turns, harness_session_id)
        return self._row(row) if row else None

    async def mark_stopped_for_swarm(self, swarm_id: str) -> int:
        result = await self._pool.execute(
            "UPDATE agent_sessions SET session_state = 'stopped', last_seen = now() "
            "WHERE swarm_id = $1 AND session_state IN ('launching','live')", swarm_id)
        return int(result.split()[-1])

    async def delete_for_swarm(self, swarm_id: str) -> None:
        await self._pool.execute("DELETE FROM agent_sessions WHERE swarm_id = $1", swarm_id)

    @staticmethod
    def _row(row: asyncpg.Record) -> AgentSession:
        return AgentSession(
            id=row["id"], swarm_id=row["swarm_id"], team_id=row["team_id"], role=row["role"],
            harness=row["harness"], model=row["model"], session_state=row["session_state"],
            error=row["error"], harness_session_id=row["harness_session_id"],
            turns=row["turns"], launched_at=row["launched_at"], last_seen=row["last_seen"])
