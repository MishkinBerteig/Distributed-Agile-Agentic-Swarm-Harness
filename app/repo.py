"""asyncpg repositories for DAASH 0.1 (docs/DESIGN-0.1.md section 5).

Every repository takes an asyncpg pool created by ``app.db.create_pool`` (jsonb
codecs registered) and returns ``app.models`` types. SQL is parameterised; the
only interpolated SQL fragments are column names drawn from fixed whitelists.
The ``embedding`` columns are never selected into models.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

import asyncpg

from app.models import (
    Agent, AgentCall, AcceptanceCriterion, Feedback, GlobalConfig, Learning, Memory,
    Swarm, SwarmConfig, SwarmSnapshot, Task, TaskCreate, TaskEvent,
    Team, TeamProposal, Verdict,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _uid() -> str:
    return str(uuid.uuid4())


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _vec_or_none(vec: Optional[list[float]]) -> Optional[str]:
    return _vec_literal(vec) if vec is not None else None


def build_update(allowed: Iterable[str], fields: Mapping[str, Any], first_param: int = 2
                 ) -> tuple[str, list[Any]]:
    """Build ``"a = $2, b = $3"`` and its params from ``fields``.

    Raises ValueError for any field name outside ``allowed`` (so user input never
    reaches the SQL text) or when ``fields`` is empty.
    """
    allowed_set = set(allowed)
    unknown = sorted(set(fields) - allowed_set)
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}")
    if not fields:
        raise ValueError("no fields to update")
    parts, params = [], []
    for i, (k, v) in enumerate(fields.items()):
        parts.append(f"{k} = ${first_param + i}")
        params.append(v)
    return ", ".join(parts), params


def compute_cost(tokens_by_model: Mapping[str, int], prices_per_1k: Mapping[str, float]) -> float:
    """USD cost: tokens x price per 1k tokens; models without a price are free."""
    return sum(t * prices_per_1k.get(m, 0.0) / 1000.0 for m, t in tokens_by_model.items())


def _to(model, row: Optional[Mapping[str, Any]], **extra):
    if row is None:
        return None
    return model(**{**dict(row), **extra})


def _dump_config(cfg: SwarmConfig | Mapping[str, Any]) -> dict:
    return cfg.model_dump(mode="json") if isinstance(cfg, SwarmConfig) else dict(cfg)


def _val(x: Any) -> Any:
    """Enum -> its value; everything else unchanged."""
    return x.value if hasattr(x, "value") else x


# ---------------------------------------------------------------------------
# Column lists (no embeddings) and update whitelists
# ---------------------------------------------------------------------------

SWARM_COLS = ("id, name, vision_statement, definition_of_done, lifecycle_state, config, "
              "paused_reason, retrospective_done, created_at, started_at")
TEAM_COLS = ("id, swarm_id, name, mission_statement, status, creator_kind, creator_team_id, "
             "definition_of_ready, definition_of_done, generation, next_focus, "
             "last_assessed_at, created_at")
AGENT_COLS = ("id, swarm_id, team_id, role, name, harness, provider, model, state, "
              "current_task_id, tokens_used, calls, errors, created_at, last_active_at")
TASK_COLS = ("id, swarm_id, team_id, parent_id, name, description, artifact, status, "
             "priority, complexity, depends_on, assigned_agent_id, attempts, created_by, "
             "rejection_reason, feedback, deliverable, created_at, updated_at, approved_at, "
             "committed_at, done_at")
CRITERION_COLS = ("id, task_id, ordinal, text, check_kind, check_path, check_arg, status, "
                  "score, evidence, updated_at")
VERDICT_COLS = "id, task_id, attempt, judge_role, agent_id, passed, score, reasoning, details, created_at"
EVENT_COLS = "id, swarm_id, team_id, task_id, kind, from_status, to_status, actor, detail, created_at"
CALL_COLS_NO_TRANSCRIPT = ("id, swarm_id, team_id, agent_id, task_id, role, model, purpose, "
                           "prompt_tokens, completion_tokens, total_tokens, duration_ms, ok, "
                           "error, created_at")
LEARNING_COLS = "id, swarm_id, team_id, task_id, kind, content, created_by, created_at"
MEMORY_COLS = "id, agent_id, swarm_id, team_id, task_id, content, created_at"
PROPOSAL_COLS = ("id, swarm_id, team_id, proposer_agent_id, name, mission_statement, reason, "
                 "status, votes, quorum, created_team_id, created_at, decided_at")

SWARM_UPDATABLE = ("name", "vision_statement", "definition_of_done", "config", "lifecycle_state",
                   "paused_reason", "retrospective_done", "started_at")
TEAM_UPDATABLE = ("name", "mission_statement", "status", "definition_of_ready",
                  "definition_of_done", "generation", "next_focus", "last_assessed_at")
TASK_UPDATABLE = ("status", "priority", "complexity", "assigned_agent_id", "attempts",
                  "rejection_reason", "feedback", "deliverable", "approved_at", "committed_at",
                  "done_at", "name", "description", "artifact")


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------

class _Repo:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool


class ConfigRepo(_Repo):
    async def get_global(self) -> GlobalConfig:
        rows = await self._pool.fetch("SELECT key, value FROM global_config")
        vals = {r["key"]: r["value"] for r in rows}
        kw: dict[str, Any] = {}
        if isinstance(vals.get("values_principles"), str):
            kw["values_principles"] = vals["values_principles"]
        if isinstance(vals.get("default_swarm_config"), dict):
            kw["default_swarm_config"] = vals["default_swarm_config"]
        return GlobalConfig(**kw)

    async def put_global(self, cfg: GlobalConfig) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            for key, val in (("values_principles", cfg.values_principles),
                             ("default_swarm_config", cfg.default_swarm_config.model_dump(mode="json"))):
                await conn.execute(
                    "INSERT INTO global_config (key, value) VALUES ($1, $2) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", key, val)


class SwarmRepo(_Repo):
    async def create(self, name: str, vision_statement: str, definition_of_done: Optional[str],
                     config: SwarmConfig) -> Swarm:
        """Raises asyncpg.UniqueViolationError (uq_one_live_swarm) if a live swarm exists."""
        row = await self._pool.fetchrow(
            f"INSERT INTO swarms (id, name, vision_statement, definition_of_done, config) "
            f"VALUES ($1, $2, $3, $4, $5) RETURNING {SWARM_COLS}",
            _uid(), name, vision_statement, definition_of_done, _dump_config(config))
        return _to(Swarm, row)

    async def get(self, swarm_id: str) -> Optional[Swarm]:
        return _to(Swarm, await self._pool.fetchrow(
            f"SELECT {SWARM_COLS} FROM swarms WHERE id = $1", swarm_id))

    async def list(self) -> list[Swarm]:
        rows = await self._pool.fetch(f"SELECT {SWARM_COLS} FROM swarms ORDER BY created_at DESC")
        return [_to(Swarm, r) for r in rows]

    async def get_live(self) -> Optional[Swarm]:
        row = await self._pool.fetchrow(
            f"SELECT {SWARM_COLS} FROM swarms WHERE lifecycle_state NOT IN ('ARCHIVED','DELETED') "
            f"ORDER BY created_at DESC LIMIT 1")
        return _to(Swarm, row)

    async def update(self, swarm_id: str, **fields: Any) -> Optional[Swarm]:
        if fields.get("config") is not None:
            fields["config"] = _dump_config(fields["config"])
        if "lifecycle_state" in fields:
            fields["lifecycle_state"] = _val(fields["lifecycle_state"])
        sets, params = build_update(SWARM_UPDATABLE, fields)
        row = await self._pool.fetchrow(
            f"UPDATE swarms SET {sets} WHERE id = $1 RETURNING {SWARM_COLS}", swarm_id, *params)
        return _to(Swarm, row)

    async def delete(self, swarm_id: str) -> bool:
        res = await self._pool.execute("DELETE FROM swarms WHERE id = $1", swarm_id)
        return res.endswith(" 1")


class TeamRepo(_Repo):
    async def create(self, swarm_id: str, name: str, mission_statement: str,
                     creator_kind: str = "user", creator_team_id: Optional[str] = None) -> Team:
        row = await self._pool.fetchrow(
            f"INSERT INTO teams (id, swarm_id, name, mission_statement, creator_kind, creator_team_id) "
            f"VALUES ($1, $2, $3, $4, $5, $6) RETURNING {TEAM_COLS}",
            _uid(), swarm_id, name, mission_statement, _val(creator_kind), creator_team_id)
        return _to(Team, row)

    async def get(self, team_id: str) -> Optional[Team]:
        return _to(Team, await self._pool.fetchrow(
            f"SELECT {TEAM_COLS} FROM teams WHERE id = $1", team_id))

    async def list(self, swarm_id: str) -> list[Team]:
        rows = await self._pool.fetch(
            f"SELECT {TEAM_COLS} FROM teams WHERE swarm_id = $1 ORDER BY created_at, id", swarm_id)
        return [_to(Team, r) for r in rows]

    async def update(self, team_id: str, **fields: Any) -> Optional[Team]:
        if "status" in fields:
            fields["status"] = _val(fields["status"])
        sets, params = build_update(TEAM_UPDATABLE, fields)
        row = await self._pool.fetchrow(
            f"UPDATE teams SET {sets} WHERE id = $1 RETURNING {TEAM_COLS}", team_id, *params)
        return _to(Team, row)

    async def delete(self, team_id: str) -> bool:
        res = await self._pool.execute("DELETE FROM teams WHERE id = $1", team_id)
        return res.endswith(" 1")


class AgentRepo(_Repo):
    async def create(self, swarm_id: str, team_id: Optional[str], role: str, name: str,
                     model: str, harness: str = "daash", provider: str = "default") -> Agent:
        row = await self._pool.fetchrow(
            f"INSERT INTO agents (id, swarm_id, team_id, role, name, model, harness, provider) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING {AGENT_COLS}",
            _uid(), swarm_id, team_id, _val(role), name, model, harness, provider)
        return _to(Agent, row)

    async def get(self, agent_id: str) -> Optional[Agent]:
        return _to(Agent, await self._pool.fetchrow(
            f"SELECT {AGENT_COLS} FROM agents WHERE id = $1", agent_id))

    async def list(self, swarm_id: str, team_id: Optional[str] = None) -> list[Agent]:
        if team_id is None:
            rows = await self._pool.fetch(
                f"SELECT {AGENT_COLS} FROM agents WHERE swarm_id = $1 ORDER BY created_at, name", swarm_id)
        else:
            rows = await self._pool.fetch(
                f"SELECT {AGENT_COLS} FROM agents WHERE swarm_id = $1 AND team_id = $2 "
                f"ORDER BY created_at, name", swarm_id, team_id)
        return [_to(Agent, r) for r in rows]

    async def set_state(self, agent_id: str, state: str,
                        current_task_id: Optional[str] = None) -> Optional[Agent]:
        row = await self._pool.fetchrow(
            f"UPDATE agents SET state = $2, current_task_id = $3 WHERE id = $1 RETURNING {AGENT_COLS}",
            agent_id, _val(state), current_task_id)
        return _to(Agent, row)

    async def record_usage(self, agent_id: str, tokens: int, ok: bool) -> None:
        await self._pool.execute(
            "UPDATE agents SET tokens_used = tokens_used + $2, calls = calls + 1, "
            "errors = errors + $3, last_active_at = now() WHERE id = $1",
            agent_id, int(tokens), 0 if ok else 1)

    async def reset_busy(self) -> int:
        res = await self._pool.execute(
            "UPDATE agents SET state = 'idle', current_task_id = NULL WHERE state = 'busy'")
        return int(res.split()[-1])


class TaskRepo(_Repo):
    async def _attach_criteria(self, rows: list[Mapping[str, Any]]) -> list[Task]:
        """One extra query for all tasks' criteria (no N+1)."""
        if not rows:
            return []
        crows = await self._pool.fetch(
            f"SELECT {CRITERION_COLS} FROM acceptance_criteria WHERE task_id = ANY($1::text[]) "
            f"ORDER BY task_id, ordinal", [r["id"] for r in rows])
        by_task: dict[str, list[AcceptanceCriterion]] = {}
        for c in crows:
            by_task.setdefault(c["task_id"], []).append(_to(AcceptanceCriterion, c))
        return [_to(Task, r, criteria=by_task.get(r["id"], [])) for r in rows]

    async def create(self, swarm_id: str, data: TaskCreate, created_by: str = "user",
                     embedding: Optional[list[float]] = None) -> Task:
        task_id = _uid()
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO tasks (id, swarm_id, team_id, parent_id, name, description, artifact, "
                "depends_on, created_by, embedding) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::vector)",
                task_id, swarm_id, data.team_id, data.parent_id, data.name, data.description,
                data.artifact, list(data.depends_on), created_by, _vec_or_none(embedding))
            for i, c in enumerate(data.acceptance_criteria, start=1):
                await conn.execute(
                    "INSERT INTO acceptance_criteria (id, task_id, ordinal, text, check_kind, "
                    "check_path, check_arg) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    _uid(), task_id, i, c.text, _val(c.check_kind), c.check_path, c.check_arg)
        return await self.get(task_id)

    async def get(self, task_id: str) -> Optional[Task]:
        row = await self._pool.fetchrow(f"SELECT {TASK_COLS} FROM tasks WHERE id = $1", task_id)
        if row is None:
            return None
        return (await self._attach_criteria([row]))[0]

    async def list(self, swarm_id: Optional[str] = None, team_id: Optional[str] = None,
                   status: Optional[str] = None) -> list[Task]:
        conds, params = [], []
        for col, val in (("swarm_id", swarm_id), ("team_id", team_id), ("status", status)):
            if val is not None:
                params.append(_val(val))
                conds.append(f"{col} = ${len(params)}")
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = await self._pool.fetch(
            f"SELECT {TASK_COLS} FROM tasks{where} ORDER BY created_at, id", *params)
        return await self._attach_criteria(rows)

    async def update(self, task_id: str, **fields: Any) -> Optional[Task]:
        if "status" in fields:
            fields["status"] = _val(fields["status"])
        if "complexity" in fields:
            fields["complexity"] = _val(fields["complexity"])
        sets, params = build_update(TASK_UPDATABLE, fields)
        row = await self._pool.fetchrow(
            f"UPDATE tasks SET {sets}, updated_at = now() WHERE id = $1 RETURNING {TASK_COLS}",
            task_id, *params)
        if row is None:
            return None
        return (await self._attach_criteria([row]))[0]

    async def update_criterion(self, criterion_id: str, status: str, score: Optional[float],
                               evidence: Optional[str]) -> Optional[AcceptanceCriterion]:
        row = await self._pool.fetchrow(
            f"UPDATE acceptance_criteria SET status = $2, score = $3, evidence = $4, "
            f"updated_at = now() WHERE id = $1 RETURNING {CRITERION_COLS}",
            criterion_id, _val(status), score, evidence)
        return _to(AcceptanceCriterion, row)

    async def children(self, task_id: str) -> list[Task]:
        rows = await self._pool.fetch(
            f"SELECT {TASK_COLS} FROM tasks WHERE parent_id = $1 ORDER BY created_at, id", task_id)
        return await self._attach_criteria(rows)

    async def delete(self, task_id: str) -> bool:
        res = await self._pool.execute("DELETE FROM tasks WHERE id = $1", task_id)
        return res.endswith(" 1")


class VerdictRepo(_Repo):
    async def add(self, task_id: str, attempt: int, judge_role: str, agent_id: Optional[str],
                  passed: bool, score: float, reasoning: str = "",
                  details: Optional[dict] = None) -> Verdict:
        row = await self._pool.fetchrow(
            f"INSERT INTO verdicts (id, task_id, attempt, judge_role, agent_id, passed, score, "
            f"reasoning, details) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING {VERDICT_COLS}",
            _uid(), task_id, attempt, _val(judge_role), agent_id, passed, float(score), reasoning,
            details or {})
        return _to(Verdict, row)

    async def list_for_task(self, task_id: str) -> list[Verdict]:
        rows = await self._pool.fetch(
            f"SELECT {VERDICT_COLS} FROM verdicts WHERE task_id = $1 ORDER BY created_at, id", task_id)
        return [_to(Verdict, r) for r in rows]


class EventRepo(_Repo):
    async def add(self, swarm_id: str, kind: str, detail: str = "", team_id: Optional[str] = None,
                  task_id: Optional[str] = None, from_status: Optional[str] = None,
                  to_status: Optional[str] = None, actor: str = "engine") -> TaskEvent:
        row = await self._pool.fetchrow(
            f"INSERT INTO task_events (swarm_id, team_id, task_id, kind, from_status, to_status, "
            f"actor, detail) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING {EVENT_COLS}",
            swarm_id, team_id, task_id, kind, _val(from_status), _val(to_status), actor, detail)
        return _to(TaskEvent, row)

    async def list(self, swarm_id: str, limit: int = 100, team_id: Optional[str] = None,
                   task_id: Optional[str] = None) -> list[TaskEvent]:
        """Newest first."""
        conds, params = ["swarm_id = $1"], [swarm_id]
        for col, val in (("team_id", team_id), ("task_id", task_id)):
            if val is not None:
                params.append(val)
                conds.append(f"{col} = ${len(params)}")
        params.append(int(limit))
        rows = await self._pool.fetch(
            f"SELECT {EVENT_COLS} FROM task_events WHERE {' AND '.join(conds)} "
            f"ORDER BY created_at DESC, id DESC LIMIT ${len(params)}", *params)
        return [_to(TaskEvent, r) for r in rows]

    async def list_since(self, swarm_id: str, since: Optional[datetime]) -> list[TaskEvent]:
        """Oldest first; ``since`` is inclusive, None = all."""
        if since is None:
            rows = await self._pool.fetch(
                f"SELECT {EVENT_COLS} FROM task_events WHERE swarm_id = $1 ORDER BY created_at, id",
                swarm_id)
        else:
            rows = await self._pool.fetch(
                f"SELECT {EVENT_COLS} FROM task_events WHERE swarm_id = $1 AND created_at >= $2 "
                f"ORDER BY created_at, id", swarm_id, since)
        return [_to(TaskEvent, r) for r in rows]


class CallRepo(_Repo):
    async def add(self, swarm_id: str, role: str, model: str, purpose: str, *,
                  team_id: Optional[str] = None, agent_id: Optional[str] = None,
                  task_id: Optional[str] = None, prompt_tokens: int = 0,
                  completion_tokens: int = 0, total_tokens: int = 0, duration_ms: int = 0,
                  ok: bool = True, error: Optional[str] = None,
                  transcript: Optional[list] = None) -> AgentCall:
        row = await self._pool.fetchrow(
            "INSERT INTO agent_calls (id, swarm_id, team_id, agent_id, task_id, role, model, purpose, "
            "prompt_tokens, completion_tokens, total_tokens, duration_ms, ok, error, transcript) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) "
            f"RETURNING {CALL_COLS_NO_TRANSCRIPT}, transcript",
            _uid(), swarm_id, team_id, agent_id, task_id, _val(role), model, _val(purpose),
            prompt_tokens, completion_tokens, total_tokens, duration_ms, ok, error,
            transcript or [])
        return _to(AgentCall, row)

    async def list_for_agent(self, agent_id: str, limit: int = 20) -> list[AgentCall]:
        """Newest first, with transcripts."""
        rows = await self._pool.fetch(
            f"SELECT {CALL_COLS_NO_TRANSCRIPT}, transcript FROM agent_calls WHERE agent_id = $1 "
            f"ORDER BY created_at DESC, id LIMIT $2", agent_id, int(limit))
        return [_to(AgentCall, r) for r in rows]

    async def list_for_swarm(self, swarm_id: str) -> list[AgentCall]:
        """Oldest first, WITHOUT transcripts (they can be huge); transcript is []."""
        rows = await self._pool.fetch(
            f"SELECT {CALL_COLS_NO_TRANSCRIPT} FROM agent_calls WHERE swarm_id = $1 "
            f"ORDER BY created_at, id", swarm_id)
        return [_to(AgentCall, r) for r in rows]

    async def tokens_by_model(self, swarm_id: str) -> dict[str, int]:
        rows = await self._pool.fetch(
            "SELECT model, COALESCE(SUM(total_tokens), 0)::bigint AS t FROM agent_calls "
            "WHERE swarm_id = $1 GROUP BY model", swarm_id)
        return {r["model"]: int(r["t"]) for r in rows}

    async def tokens_used(self, swarm_id: str) -> int:
        return int(await self._pool.fetchval(
            "SELECT COALESCE(SUM(total_tokens), 0)::bigint FROM agent_calls WHERE swarm_id = $1",
            swarm_id))


class LearningRepo(_Repo):
    async def add(self, content: str, kind: str, swarm_id: Optional[str] = None,
                  team_id: Optional[str] = None, task_id: Optional[str] = None,
                  created_by: str = "engine",
                  embedding: Optional[list[float]] = None) -> Learning:
        row = await self._pool.fetchrow(
            f"INSERT INTO learnings (id, swarm_id, team_id, task_id, kind, content, created_by, "
            f"embedding) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector) RETURNING {LEARNING_COLS}",
            _uid(), swarm_id, team_id, task_id, _val(kind), content, created_by,
            _vec_or_none(embedding))
        return _to(Learning, row)

    @staticmethod
    def _scope(include_global: bool) -> str:
        return "(swarm_id = $1 OR swarm_id IS NULL)" if include_global else "swarm_id = $1"

    async def list(self, swarm_id: str, include_global: bool = True, limit: int = 50) -> list[Learning]:
        """Newest first."""
        rows = await self._pool.fetch(
            f"SELECT {LEARNING_COLS} FROM learnings WHERE {self._scope(include_global)} "
            f"ORDER BY created_at DESC, id LIMIT $2", swarm_id, int(limit))
        return [_to(Learning, r) for r in rows]

    async def search(self, embedding: Optional[list[float]], swarm_id: str, k: int = 8,
                     include_global: bool = True) -> list[Learning]:
        """Nearest by cosine distance; recency when ``embedding`` is None."""
        if embedding is None:
            return await self.list(swarm_id, include_global, k)
        rows = await self._pool.fetch(
            f"SELECT {LEARNING_COLS} FROM learnings WHERE {self._scope(include_global)} "
            f"ORDER BY embedding <=> $2::vector NULLS LAST, created_at DESC LIMIT $3",
            swarm_id, _vec_literal(embedding), int(k))
        return [_to(Learning, r) for r in rows]


class MemoryRepo(_Repo):
    async def add(self, agent_id: str, swarm_id: str, content: str, team_id: Optional[str] = None,
                  task_id: Optional[str] = None,
                  embedding: Optional[list[float]] = None) -> Memory:
        row = await self._pool.fetchrow(
            f"INSERT INTO memories (id, agent_id, swarm_id, team_id, task_id, content, embedding) "
            f"VALUES ($1,$2,$3,$4,$5,$6,$7::vector) RETURNING {MEMORY_COLS}",
            _uid(), agent_id, swarm_id, team_id, task_id, content, _vec_or_none(embedding))
        return _to(Memory, row)

    async def list(self, agent_id: str, limit: int = 50) -> list[Memory]:
        """Newest first."""
        rows = await self._pool.fetch(
            f"SELECT {MEMORY_COLS} FROM memories WHERE agent_id = $1 "
            f"ORDER BY created_at DESC, id LIMIT $2", agent_id, int(limit))
        return [_to(Memory, r) for r in rows]

    async def search(self, agent_id: str, embedding: Optional[list[float]], k: int = 8) -> list[Memory]:
        if embedding is None:
            return await self.list(agent_id, k)
        rows = await self._pool.fetch(
            f"SELECT {MEMORY_COLS} FROM memories WHERE agent_id = $1 "
            f"ORDER BY embedding <=> $2::vector NULLS LAST, created_at DESC LIMIT $3",
            agent_id, _vec_literal(embedding), int(k))
        return [_to(Memory, r) for r in rows]


class ProposalRepo(_Repo):
    async def create(self, swarm_id: str, team_id: str, proposer_agent_id: str, name: str,
                     mission_statement: str, reason: str, quorum: int) -> TeamProposal:
        row = await self._pool.fetchrow(
            f"INSERT INTO team_proposals (id, swarm_id, team_id, proposer_agent_id, name, "
            f"mission_statement, reason, quorum) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
            f"RETURNING {PROPOSAL_COLS}",
            _uid(), swarm_id, team_id, proposer_agent_id, name, mission_statement, reason, quorum)
        return _to(TeamProposal, row)

    async def get(self, proposal_id: str) -> Optional[TeamProposal]:
        return _to(TeamProposal, await self._pool.fetchrow(
            f"SELECT {PROPOSAL_COLS} FROM team_proposals WHERE id = $1", proposal_id))

    async def list(self, swarm_id: str, status: Optional[str] = None) -> list[TeamProposal]:
        if status is None:
            rows = await self._pool.fetch(
                f"SELECT {PROPOSAL_COLS} FROM team_proposals WHERE swarm_id = $1 "
                f"ORDER BY created_at, id", swarm_id)
        else:
            rows = await self._pool.fetch(
                f"SELECT {PROPOSAL_COLS} FROM team_proposals WHERE swarm_id = $1 AND status = $2 "
                f"ORDER BY created_at, id", swarm_id, _val(status))
        return [_to(TeamProposal, r) for r in rows]

    async def record_vote(self, proposal_id: str, agent_id: str, approve: bool) -> Optional[TeamProposal]:
        """Atomically record (or overwrite) one agent's vote; returns the updated proposal."""
        async with self._pool.acquire() as conn, conn.transaction():
            locked = await conn.fetchval(
                "SELECT id FROM team_proposals WHERE id = $1 FOR UPDATE", proposal_id)
            if locked is None:
                return None
            row = await conn.fetchrow(
                f"UPDATE team_proposals SET votes = votes || jsonb_build_object($2::text, $3::boolean) "
                f"WHERE id = $1 RETURNING {PROPOSAL_COLS}", proposal_id, agent_id, approve)
        return _to(TeamProposal, row)

    async def set_status(self, proposal_id: str, status: str,
                         created_team_id: Optional[str] = None) -> Optional[TeamProposal]:
        row = await self._pool.fetchrow(
            f"UPDATE team_proposals SET status = $2, created_team_id = COALESCE($3, created_team_id), "
            f"decided_at = now() WHERE id = $1 RETURNING {PROPOSAL_COLS}",
            proposal_id, _val(status), created_team_id)
        return _to(TeamProposal, row)


class FeedbackRepo(_Repo):
    async def add(self, swarm_id: str, text: str) -> Feedback:
        row = await self._pool.fetchrow(
            "INSERT INTO feedback (id, swarm_id, text) VALUES ($1,$2,$3) "
            "RETURNING id, swarm_id, text, created_at", _uid(), swarm_id, text)
        return _to(Feedback, row)

    async def list(self, swarm_id: str) -> list[Feedback]:
        """Oldest first."""
        rows = await self._pool.fetch(
            "SELECT id, swarm_id, text, created_at FROM feedback WHERE swarm_id = $1 "
            "ORDER BY created_at, id", swarm_id)
        return [_to(Feedback, r) for r in rows]


# ---------------------------------------------------------------------------
# Engine snapshot
# ---------------------------------------------------------------------------

_P90_SQL = """
SELECT count(*) AS n,
       (percentile_disc(0.9) WITHIN GROUP (ORDER BY secs))::double precision AS p90
FROM (
    SELECT extract(epoch FROM done_at - committed_at)::double precision AS secs
    FROM tasks
    WHERE swarm_id = $1 AND status = 'Done' AND done_at IS NOT NULL AND committed_at IS NOT NULL
    ORDER BY done_at DESC
    LIMIT 100
) recent
"""


async def load_snapshot(pool: asyncpg.Pool, swarm_id: str, now: datetime,
                        running_jobs: set[str]) -> Optional[SwarmSnapshot]:
    """Everything the pure ``decide()`` needs for one swarm; None if the swarm is gone."""
    swarm = await SwarmRepo(pool).get(swarm_id)
    if swarm is None:
        return None
    teams = await TeamRepo(pool).list(swarm_id)
    agents = await AgentRepo(pool).list(swarm_id)
    tasks = await TaskRepo(pool).list(swarm_id=swarm_id)
    proposals = await ProposalRepo(pool).list(swarm_id)
    by_model = await CallRepo(pool).tokens_by_model(swarm_id)
    p90row = await pool.fetchrow(_P90_SQL, swarm_id)
    p90 = p90row["p90"] if p90row and p90row["n"] >= 5 else None
    return SwarmSnapshot(
        swarm=swarm, teams=teams, agents=agents, tasks=tasks, proposals=proposals,
        p90_cycle_seconds=p90, tokens_used=sum(by_model.values()),
        cost_usd=compute_cost(by_model, swarm.config.model_prices),
        now=now, running_jobs=set(running_jobs))
