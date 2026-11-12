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
    CONNECTED_HARNESS, Agent, AgentCall, AgentMessage, AcceptanceCriterion, ConnectedAgent,
    Feedback, GlobalConfig, Learning, Memory, RemoteJob, Runner, Swarm, SwarmConfig,
    SwarmSnapshot, Task, TaskCreate, TaskEvent, Team, TeamProposal, Verdict, harness_capabilities,
    normalize_capabilities, required_capabilities, with_command_capability,
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
              "paused_reason, retrospective_done, vision_reviews, created_at, started_at")
TEAM_COLS = ("id, swarm_id, name, mission_statement, status, creator_kind, creator_team_id, "
             "definition_of_ready, definition_of_done, generation, next_focus, "
             "last_assessed_at, created_at")
AGENT_COLS = ("id, swarm_id, team_id, role, name, harness, capabilities, provider, model, state, "
              "current_task_id, tokens_used, calls, errors, created_at, last_active_at")
TASK_COLS = ("id, swarm_id, team_id, parent_id, name, description, artifact, status, "
             "priority, complexity, depends_on, required_capabilities, assigned_agent_id, attempts, created_by, "
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
                   "paused_reason", "retrospective_done", "vision_reviews", "started_at")
TEAM_UPDATABLE = ("name", "mission_statement", "status", "definition_of_ready",
                  "definition_of_done", "generation", "next_focus", "last_assessed_at")
TASK_UPDATABLE = ("status", "priority", "complexity", "assigned_agent_id", "attempts",
                  "rejection_reason", "feedback", "deliverable", "approved_at", "committed_at",
                  "done_at", "name", "description", "artifact", "required_capabilities")


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
                     model: str, harness: str = "daash", provider: str = "default",
                     capabilities: Optional[list[str]] = None) -> Agent:
        caps = harness_capabilities(harness, capabilities)
        row = await self._pool.fetchrow(
            f"INSERT INTO agents (id, swarm_id, team_id, role, name, model, harness, provider, "
            f"capabilities) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING {AGENT_COLS}",
            _uid(), swarm_id, team_id, _val(role), name, model, harness, provider, caps)
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
                "depends_on, required_capabilities, created_by, embedding) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::vector)",
                task_id, swarm_id, data.team_id, data.parent_id, data.name, data.description,
                data.artifact, list(data.depends_on),
                with_command_capability(data.required_capabilities, data.acceptance_criteria),
                created_by, _vec_or_none(embedding))
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
# Runners and remote jobs (docs/DESIGN-0.3.md section 3)
# ---------------------------------------------------------------------------

RUNNER_COLS = ("id, name, token_hash, enabled, host, version, harnesses, max_jobs, "
               "last_seen_at, created_at")
REMOTE_JOB_COLS = ("id, swarm_id, team_id, task_id, agent_id, attempt, harness, model, status, "
                   "runner_id, payload, result, expiries, lease_expires_at, heartbeat_at, "
                   "created_at, leased_at, finished_at")
RUNNER_UPDATABLE = ("name", "token_hash", "enabled", "host", "version", "harnesses", "max_jobs")
LEASE_CANDIDATES = 25   # oldest queued jobs examined per lease attempt


class RunnerRepo(_Repo):
    async def create(self, name: str, token_hash: str, max_jobs: int = 1) -> Runner:
        """Raises asyncpg.UniqueViolationError if the name or token hash exists."""
        row = await self._pool.fetchrow(
            f"INSERT INTO runners (id, name, token_hash, max_jobs) VALUES ($1, $2, $3, $4) "
            f"RETURNING {RUNNER_COLS}", _uid(), name, token_hash, max_jobs)
        return _to(Runner, row)

    async def get(self, runner_id: str) -> Optional[Runner]:
        return _to(Runner, await self._pool.fetchrow(
            f"SELECT {RUNNER_COLS} FROM runners WHERE id = $1", runner_id))

    async def get_by_name(self, name: str) -> Optional[Runner]:
        return _to(Runner, await self._pool.fetchrow(
            f"SELECT {RUNNER_COLS} FROM runners WHERE name = $1", name))

    async def by_token_hash(self, token_hash: str) -> Optional[Runner]:
        return _to(Runner, await self._pool.fetchrow(
            f"SELECT {RUNNER_COLS} FROM runners WHERE token_hash = $1", token_hash))

    async def list(self) -> list[Runner]:
        rows = await self._pool.fetch(f"SELECT {RUNNER_COLS} FROM runners ORDER BY created_at, name")
        return [_to(Runner, r) for r in rows]

    async def update(self, runner_id: str, **fields: Any) -> Optional[Runner]:
        sets, params = build_update(RUNNER_UPDATABLE, fields)
        row = await self._pool.fetchrow(
            f"UPDATE runners SET {sets} WHERE id = $1 RETURNING {RUNNER_COLS}", runner_id, *params)
        return _to(Runner, row)

    async def touch(self, runner_id: str) -> None:
        await self._pool.execute("UPDATE runners SET last_seen_at = now() WHERE id = $1", runner_id)

    async def delete(self, runner_id: str) -> bool:
        """Revoke a runner; its leased jobs go back to the queue first."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE remote_jobs SET status = 'queued', runner_id = NULL, "
                "lease_expires_at = NULL WHERE runner_id = $1 AND status = 'leased'", runner_id)
            res = await conn.execute("DELETE FROM runners WHERE id = $1", runner_id)
        return res.endswith(" 1")

    async def ensure_bootstrap(self, name: str, token_hash: str) -> Runner:
        """The runner `name` with exactly this token hash (created, or its hash updated)."""
        existing = await self.get_by_name(name)
        if existing is None:
            return await self.create(name, token_hash)
        if existing.token_hash != token_hash:
            return await self.update(existing.id, token_hash=token_hash, enabled=True)
        return existing

    async def active_job_counts(self) -> dict[str, int]:
        rows = await self._pool.fetch(
            "SELECT runner_id, count(*) AS n FROM remote_jobs WHERE status = 'leased' "
            "AND runner_id IS NOT NULL GROUP BY runner_id")
        return {r["runner_id"]: r["n"] for r in rows}


class RemoteJobRepo(_Repo):
    async def ensure_active(self, swarm_id: str, team_id: str, task_id: str, agent_id: str,
                            attempt: int, harness: str, model: str,
                            payload: dict) -> RemoteJob:
        """The task's queued/leased job, or a new queued one (an engine restart re-attaches)."""
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                f"SELECT {REMOTE_JOB_COLS} FROM remote_jobs WHERE task_id = $1 "
                f"AND status IN ('queued', 'leased')", task_id)
            if row is None:
                row = await conn.fetchrow(
                    f"INSERT INTO remote_jobs (id, swarm_id, team_id, task_id, agent_id, attempt, "
                    f"harness, model, payload) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
                    f"RETURNING {REMOTE_JOB_COLS}",
                    _uid(), swarm_id, team_id, task_id, agent_id, attempt, harness, model, payload)
        return _to(RemoteJob, row)

    async def latest_for_task(self, task_id: str) -> Optional[RemoteJob]:
        return _to(RemoteJob, await self._pool.fetchrow(
            f"SELECT {REMOTE_JOB_COLS} FROM remote_jobs WHERE task_id = $1 "
            f"ORDER BY created_at DESC LIMIT 1", task_id))

    async def get(self, job_id: str) -> Optional[RemoteJob]:
        return _to(RemoteJob, await self._pool.fetchrow(
            f"SELECT {REMOTE_JOB_COLS} FROM remote_jobs WHERE id = $1", job_id))

    async def list(self, swarm_id: Optional[str] = None, limit: int = 200) -> list[RemoteJob]:
        if swarm_id is None:
            rows = await self._pool.fetch(
                f"SELECT {REMOTE_JOB_COLS} FROM remote_jobs ORDER BY created_at DESC LIMIT $1", limit)
        else:
            rows = await self._pool.fetch(
                f"SELECT {REMOTE_JOB_COLS} FROM remote_jobs WHERE swarm_id = $1 "
                f"ORDER BY created_at DESC LIMIT $2", swarm_id, limit)
        return [_to(RemoteJob, r) for r in rows]

    async def requeue_expired(self, now: datetime) -> int:
        """Leased jobs whose lease timed out (runner vanished) go back to the queue. Connected
        jobs (docs/DESIGN-0.4.md section 5.5) have no runner lease and are never requeued."""
        res = await self._pool.execute(
            "UPDATE remote_jobs SET status = 'queued', runner_id = NULL, leased_at = NULL, "
            "lease_expires_at = NULL, expiries = expiries + 1 "
            "WHERE status = 'leased' AND lease_expires_at < $1 AND harness <> $2",
            now, CONNECTED_HARNESS)
        return int(res.split()[-1])

    async def lease_next(self, runner: Runner, harness_models: dict[str, list[str]],
                         now: datetime, lease_seconds: int = 60,
                         harness_caps: Optional[dict[str, list[str]]] = None,
                         mcp_token_hash: Optional[str] = None) -> Optional[RemoteJob]:
        """Lease the oldest queued job of an ACTIVE swarm that the runner can take, storing the
        hash of the job's new MCP token (docs/DESIGN-0.4.md section 4.1) in the same update.

        FOR UPDATE SKIP LOCKED: concurrent runners never wait for, or double-lease, a job."""
        from app.remote import first_match  # local import: keeps repo importable without it
        harness_caps = harness_caps or {}
        if not harness_models:
            return None
        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                f"SELECT j.id, j.harness, j.model, j.payload FROM remote_jobs j "
                f"JOIN swarms s ON s.id = j.swarm_id "
                f"WHERE j.status = 'queued' AND s.lifecycle_state = 'ACTIVE' "
                f"AND j.harness = ANY($1::text[]) AND j.harness <> $2 ORDER BY j.created_at, j.id "
                f"FOR UPDATE OF j SKIP LOCKED LIMIT {LEASE_CANDIDATES}", list(harness_models),
                CONNECTED_HARNESS)
            pick = first_match([dict(r) for r in rows], harness_models, harness_caps)
            if pick is None:
                return None
            row = await conn.fetchrow(
                f"UPDATE remote_jobs SET status = 'leased', runner_id = $2, "
                f"leased_at = $3::timestamptz, heartbeat_at = $3::timestamptz, "
                f"lease_expires_at = $3::timestamptz + make_interval(secs => $4::double precision), "
                f"mcp_token_hash = $5 "
                f"WHERE id = $1 RETURNING {REMOTE_JOB_COLS}",
                pick["id"], runner.id, now, float(lease_seconds), mcp_token_hash)
        return _to(RemoteJob, row)

    async def heartbeat(self, job_id: str, runner_id: str, now: datetime,
                        lease_seconds: int = 60) -> Optional[RemoteJob]:
        """Extend the lease of a job this runner holds; the job as it is (None if not theirs)."""
        row = await self._pool.fetchrow(
            f"UPDATE remote_jobs SET heartbeat_at = $3::timestamptz, lease_expires_at = CASE WHEN "
            f"status = 'leased' THEN $3::timestamptz + make_interval(secs => $4::double precision) "
            f"ELSE lease_expires_at END "
            f"WHERE id = $1 AND runner_id = $2 RETURNING {REMOTE_JOB_COLS}",
            job_id, runner_id, now, float(lease_seconds))
        return _to(RemoteJob, row)

    async def complete(self, job_id: str, runner_id: str, result: dict) -> Optional[RemoteJob]:
        """Store the result of a job this runner still leases; None otherwise (-> 409)."""
        row = await self._pool.fetchrow(
            f"UPDATE remote_jobs SET status = 'completed', result = $3, finished_at = now() "
            f"WHERE id = $1 AND runner_id = $2 AND status = 'leased' RETURNING {REMOTE_JOB_COLS}",
            job_id, runner_id, result)
        return _to(RemoteJob, row)

    async def _close(self, job_id: str, status: str) -> Optional[RemoteJob]:
        row = await self._pool.fetchrow(
            f"UPDATE remote_jobs SET status = $2, finished_at = now() "
            f"WHERE id = $1 AND status IN ('queued', 'leased') RETURNING {REMOTE_JOB_COLS}",
            job_id, status)
        return _to(RemoteJob, row)

    async def cancel(self, job_id: str) -> Optional[RemoteJob]:
        """The swarm/task moved on: the runner learns it from its next heartbeat."""
        return await self._close(job_id, "cancelled")

    async def abandon(self, job_id: str) -> Optional[RemoteJob]:
        """The coordinator gave up on the job (no runner, runner kept vanishing)."""
        return await self._close(job_id, "abandoned")

    # -- DAASH MCP server (docs/DESIGN-0.4.md section 2.3) ------------------------------------------

    async def by_mcp_token_hash(self, token_hash: str) -> Optional[RemoteJob]:
        """The job a per-job MCP token (`djt_...`) was minted for, whatever its status now."""
        if not token_hash:
            return None
        return _to(RemoteJob, await self._pool.fetchrow(
            f"SELECT {REMOTE_JOB_COLS} FROM remote_jobs WHERE mcp_token_hash = $1", token_hash))

    async def active_connected(self, connected_agent_id: str) -> list[RemoteJob]:
        """Queued or leased jobs of one connected agent, oldest first."""
        rows = await self._pool.fetch(
            f"SELECT {REMOTE_JOB_COLS} FROM remote_jobs WHERE harness = $1 "
            f"AND status IN ('queued', 'leased') AND payload->>'connected_agent_id' = $2 "
            f"ORDER BY created_at, id", CONNECTED_HARNESS, connected_agent_id)
        return [_to(RemoteJob, r) for r in rows]

    async def count_mcp_call(self, job_id: str) -> None:
        """payload.mcp_calls += 1 (DAASHboard shows that an agent really uses its tools)."""
        await self._pool.execute(
            "UPDATE remote_jobs SET payload = jsonb_set(payload, '{mcp_calls}', "
            "to_jsonb(COALESCE((payload->>'mcp_calls')::int, 0) + 1)) WHERE id = $1", job_id)

    async def merge_payload(self, job_id: str, fields: dict) -> Optional[RemoteJob]:
        """payload || fields (a connected job's external id, context id, error times)."""
        row = await self._pool.fetchrow(
            f"UPDATE remote_jobs SET payload = payload || $2::jsonb WHERE id = $1 "
            f"RETURNING {REMOTE_JOB_COLS}", job_id, fields)
        return _to(RemoteJob, row)

    async def finish_connected(self, job_id: str, result: dict) -> Optional[RemoteJob]:
        """Complete a directly connected job (no runner) with its result; None if it was no
        longer active (cancelled meanwhile). From now on its MCP calls are refused."""
        row = await self._pool.fetchrow(
            f"UPDATE remote_jobs SET status = 'completed', result = $2, finished_at = now() "
            f"WHERE id = $1 AND harness = $3 AND status IN ('queued', 'leased') "
            f"RETURNING {REMOTE_JOB_COLS}", job_id, result, CONNECTED_HARNESS)
        return _to(RemoteJob, row)

    async def active_connected_counts(self) -> dict[str, int]:
        """connected agent id -> its queued/leased jobs (max_jobs, section 5.2)."""
        rows = await self._pool.fetch(
            "SELECT payload->>'connected_agent_id' AS cid, count(*) AS n FROM remote_jobs "
            "WHERE harness = $1 AND status IN ('queued', 'leased') GROUP BY 1", CONNECTED_HARNESS)
        return {r["cid"]: r["n"] for r in rows if r["cid"]}

    async def record_mcp_file(self, job_id: str, path: str, written: bool = True) -> None:
        """Add (or, for a deletion, remove) a workspace path in payload.mcp_files: the files a
        connected agent wrote through MCP during this job (part of its deliverable)."""
        if written:
            await self._pool.execute(
                "UPDATE remote_jobs SET payload = jsonb_set(payload, '{mcp_files}', CASE WHEN "
                "COALESCE(payload->'mcp_files', '[]'::jsonb) ? $2::text "
                "THEN payload->'mcp_files' "
                "ELSE COALESCE(payload->'mcp_files', '[]'::jsonb) || to_jsonb($2::text) END) "
                "WHERE id = $1", job_id, path)
        else:
            await self._pool.execute(
                "UPDATE remote_jobs SET payload = jsonb_set(payload, '{mcp_files}', "
                "COALESCE(payload->'mcp_files', '[]'::jsonb) - $2::text) WHERE id = $1",
                job_id, path)


# ---------------------------------------------------------------------------
# Team messages and connected agents (docs/DESIGN-0.4.md sections 3 and 5.1)
# ---------------------------------------------------------------------------

MESSAGE_COLS = ("m.id, m.send_id, m.swarm_id, m.team_id, m.from_agent_id, m.to_agent_id, "
                "m.audience, m.task_id, m.attempt, m.text, m.created_at, m.read_at")
_MESSAGE_JOINS = ("LEFT JOIN agents fa ON fa.id = m.from_agent_id "
                  "LEFT JOIN teams ft ON ft.id = fa.team_id "
                  "LEFT JOIN agents ta ON ta.id = m.to_agent_id")
_MESSAGE_NAMES = "fa.name AS from_name, ft.name AS from_team, ta.name AS to_name"
#: Unread messages put into one prompt or returned by one read_messages call; the rest wait.
MESSAGES_PER_READ = 30


class MessageRepo(_Repo):
    async def send(self, swarm_id: str, team_id: Optional[str], from_agent_id: str,
                   to_agent_ids: Iterable[str], text: str, audience: str = "direct",
                   task_id: Optional[str] = None,
                   attempt: Optional[int] = None) -> list[AgentMessage]:
        """One send: a row per recipient, sharing a send_id (fan-out)."""
        send_id = _uid()
        ids: list[str] = []
        async with self._pool.acquire() as conn, conn.transaction():
            for to in dict.fromkeys(to_agent_ids):
                mid = _uid()
                await conn.execute(
                    "INSERT INTO agent_messages (id, send_id, swarm_id, team_id, from_agent_id, "
                    "to_agent_id, audience, task_id, attempt, text) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                    mid, send_id, swarm_id, team_id, from_agent_id, to, audience, task_id,
                    attempt, text)
                ids.append(mid)
        return await self._by_ids(ids)

    async def _by_ids(self, ids: list[str]) -> list[AgentMessage]:
        if not ids:
            return []
        rows = await self._pool.fetch(
            f"SELECT {MESSAGE_COLS}, {_MESSAGE_NAMES} FROM agent_messages m {_MESSAGE_JOINS} "
            f"WHERE m.id = ANY($1::text[]) ORDER BY m.created_at, m.id", ids)
        return [_to(AgentMessage, r) for r in rows]

    async def unread(self, agent_id: str, limit: int = MESSAGES_PER_READ) -> list[AgentMessage]:
        """Unread messages to the agent, oldest first (not marked read)."""
        rows = await self._pool.fetch(
            f"SELECT {MESSAGE_COLS}, {_MESSAGE_NAMES} FROM agent_messages m {_MESSAGE_JOINS} "
            f"WHERE m.to_agent_id = $1 AND m.read_at IS NULL "
            f"ORDER BY m.created_at, m.id LIMIT $2", agent_id, int(limit))
        return [_to(AgentMessage, r) for r in rows]

    async def mark_read(self, ids: Iterable[str]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        res = await self._pool.execute(
            "UPDATE agent_messages SET read_at = now() WHERE id = ANY($1::text[]) "
            "AND read_at IS NULL", ids)
        return int(res.split()[-1])

    async def mark_unread(self, ids: Iterable[str]) -> int:
        """Undo mark_read (a prompt that carried the messages never reached an agent)."""
        ids = list(ids)
        if not ids:
            return 0
        res = await self._pool.execute(
            "UPDATE agent_messages SET read_at = NULL WHERE id = ANY($1::text[])", ids)
        return int(res.split()[-1])

    async def take_unread(self, agent_id: str,
                          limit: int = MESSAGES_PER_READ) -> list[AgentMessage]:
        """Unread messages to the agent, oldest first, marked read in the same statement (two
        readers never get the same message)."""
        rows = await self._pool.fetch(
            f"WITH taken AS (UPDATE agent_messages SET read_at = now() WHERE id IN ("
            f"SELECT id FROM agent_messages WHERE to_agent_id = $1 AND read_at IS NULL "
            f"ORDER BY created_at, id LIMIT $2 FOR UPDATE SKIP LOCKED) RETURNING *) "
            f"SELECT {MESSAGE_COLS}, {_MESSAGE_NAMES} FROM taken m {_MESSAGE_JOINS} "
            f"ORDER BY m.created_at, m.id", agent_id, int(limit))
        return [_to(AgentMessage, r) for r in rows]

    async def sends_in_attempt(self, from_agent_id: str, task_id: Optional[str],
                               attempt: Optional[int]) -> int:
        """How many sends (not rows: a team message counts once) the agent made in this attempt."""
        return int(await self._pool.fetchval(
            "SELECT count(DISTINCT send_id) FROM agent_messages WHERE from_agent_id = $1 "
            "AND task_id IS NOT DISTINCT FROM $2 AND attempt IS NOT DISTINCT FROM $3",
            from_agent_id, task_id, attempt))

    async def list_for_swarm(self, swarm_id: str, limit: int = 200) -> list[AgentMessage]:
        """Newest first, one row per recipient (for DAASHboard)."""
        rows = await self._pool.fetch(
            f"SELECT {MESSAGE_COLS}, {_MESSAGE_NAMES} FROM agent_messages m {_MESSAGE_JOINS} "
            f"WHERE m.swarm_id = $1 ORDER BY m.created_at DESC, m.id LIMIT $2",
            swarm_id, int(limit))
        return [_to(AgentMessage, r) for r in rows]


CONNECTED_COLS = ("id, name, protocol, url, endpoint, skill, token, mcp_token_hash, capabilities, "
                  "max_jobs, enabled, card, last_ok_at, last_error, mcp_seen_at, created_at, "
                  "status, kind, pending_until, runner_id, local_ref")
CONNECTED_UPDATABLE = ("name", "protocol", "url", "endpoint", "skill", "token", "mcp_token_hash",
                       "capabilities", "max_jobs", "enabled", "card", "last_ok_at", "last_error",
                       "status", "kind", "pending_until", "runner_id", "local_ref")


class ConnectedAgentRepo(_Repo):
    """Connected agents (docs/DESIGN-0.4.md section 5.1)."""

    async def create(self, name: str, protocol: str, url: Optional[str], *,
                     endpoint: Optional[str] = None, skill: Optional[str] = None,
                     token: Optional[str] = None, mcp_token_hash: Optional[str] = None,
                     capabilities: Optional[list[str]] = None, max_jobs: int = 1,
                     enabled: bool = True, card: Optional[dict] = None,
                     status: str = "connected", kind: str = "other",
                     pending_until: Optional[datetime] = None, runner_id: Optional[str] = None,
                     local_ref: Optional[str] = None) -> ConnectedAgent:
        """Raises asyncpg.UniqueViolationError if the name or MCP token hash exists."""
        caps = normalize_capabilities(capabilities) if capabilities is not None else ["edit_files"]
        row = await self._pool.fetchrow(
            f"INSERT INTO connected_agents (id, name, protocol, url, endpoint, skill, token, "
            f"mcp_token_hash, capabilities, max_jobs, enabled, card, status, kind, pending_until, "
            f"runner_id, local_ref) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, "
            f"$13, $14, $15, $16, $17) RETURNING {CONNECTED_COLS}",
            _uid(), name, protocol, url, endpoint, skill, token, mcp_token_hash, caps, max_jobs,
            enabled, card, status, kind, pending_until, runner_id, local_ref)
        return _to(ConnectedAgent, row)

    async def get(self, agent_id: str) -> Optional[ConnectedAgent]:
        return _to(ConnectedAgent, await self._pool.fetchrow(
            f"SELECT {CONNECTED_COLS} FROM connected_agents WHERE id = $1", agent_id))

    async def get_by_name(self, name: str) -> Optional[ConnectedAgent]:
        """Case-insensitive."""
        return _to(ConnectedAgent, await self._pool.fetchrow(
            f"SELECT {CONNECTED_COLS} FROM connected_agents WHERE lower(name) = lower($1) "
            f"ORDER BY created_at LIMIT 1", name))

    async def by_mcp_token_hash(self, token_hash: str) -> Optional[ConnectedAgent]:
        if not token_hash:
            return None
        return _to(ConnectedAgent, await self._pool.fetchrow(
            f"SELECT {CONNECTED_COLS} FROM connected_agents WHERE mcp_token_hash = $1",
            token_hash))

    async def list(self) -> list[ConnectedAgent]:
        rows = await self._pool.fetch(
            f"SELECT {CONNECTED_COLS} FROM connected_agents ORDER BY created_at, name")
        return [_to(ConnectedAgent, r) for r in rows]

    async def list_pending(self, now: datetime) -> list[ConnectedAgent]:
        """Pending direct agents still inside their approval window (section 5.8)."""
        rows = await self._pool.fetch(
            f"SELECT {CONNECTED_COLS} FROM connected_agents WHERE status = 'pending' "
            f"AND enabled AND pending_until IS NOT NULL AND pending_until > $1 "
            f"ORDER BY created_at", now)
        return [_to(ConnectedAgent, r) for r in rows]

    async def update(self, agent_id: str, **fields: Any) -> Optional[ConnectedAgent]:
        if "capabilities" in fields and fields["capabilities"] is not None:
            fields["capabilities"] = normalize_capabilities(fields["capabilities"])
        sets, params = build_update(CONNECTED_UPDATABLE, fields)
        row = await self._pool.fetchrow(
            f"UPDATE connected_agents SET {sets} WHERE id = $1 RETURNING {CONNECTED_COLS}",
            agent_id, *params)
        return _to(ConnectedAgent, row)

    async def mark_connected(self, agent_id: str) -> Optional[ConnectedAgent]:
        """pending -> connected (the owner's approval was detected); None if it was not pending."""
        row = await self._pool.fetchrow(
            f"UPDATE connected_agents SET status = 'connected', pending_until = NULL, "
            f"last_ok_at = now(), last_error = NULL WHERE id = $1 AND status = 'pending' "
            f"RETURNING {CONNECTED_COLS}", agent_id)
        return _to(ConnectedAgent, row)

    async def record_check(self, agent_id: str, ok: bool,
                           error: Optional[str] = None) -> Optional[ConnectedAgent]:
        """The outcome of a check or a job's contact: ok sets last_ok_at and clears last_error."""
        if ok:
            row = await self._pool.fetchrow(
                f"UPDATE connected_agents SET last_ok_at = now(), last_error = NULL "
                f"WHERE id = $1 RETURNING {CONNECTED_COLS}", agent_id)
        else:
            row = await self._pool.fetchrow(
                f"UPDATE connected_agents SET last_error = $2 WHERE id = $1 "
                f"RETURNING {CONNECTED_COLS}", agent_id, (error or "unknown error")[:1000])
        return _to(ConnectedAgent, row)

    async def touch_mcp(self, agent_id: str) -> None:
        """The agent just called a DAASH tool over MCP."""
        await self._pool.execute(
            "UPDATE connected_agents SET mcp_seen_at = now() WHERE id = $1", agent_id)

    async def delete(self, agent_id: str) -> bool:
        res = await self._pool.execute("DELETE FROM connected_agents WHERE id = $1", agent_id)
        return res.endswith(" 1")


DISCOVERY_COLS = ("id, host, port, kind, status, name, url, card, detail, connected_agent_id, "
                  "first_seen_at, last_seen_at")


class DiscoveryRepo(_Repo):
    """What discovery found on the network (docs/DESIGN-0.4.md section 5.8): one row per
    (host, port), refreshed on every look."""

    async def upsert(self, host: str, port: int, kind: str, status: str, *,
                     name: Optional[str] = None, url: Optional[str] = None,
                     card: Optional[dict] = None, detail: Optional[str] = None,
                     connected_agent_id: Optional[str] = None) -> dict:
        row = await self._pool.fetchrow(
            f"INSERT INTO agent_discoveries (id, host, port, kind, status, name, url, card, "
            f"detail, connected_agent_id) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
            f"ON CONFLICT (host, port) DO UPDATE SET kind = EXCLUDED.kind, "
            f"status = EXCLUDED.status, name = EXCLUDED.name, url = EXCLUDED.url, "
            f"card = EXCLUDED.card, detail = EXCLUDED.detail, "
            f"connected_agent_id = COALESCE(EXCLUDED.connected_agent_id, "
            f"agent_discoveries.connected_agent_id), last_seen_at = now() "
            f"RETURNING {DISCOVERY_COLS}",
            _uid(), host, int(port), kind, status, name, url, card, detail, connected_agent_id)
        return dict(row)

    async def list(self) -> list[dict]:
        rows = await self._pool.fetch(
            f"SELECT {DISCOVERY_COLS} FROM agent_discoveries "
            f"ORDER BY status, last_seen_at DESC, host, port")
        return [dict(r) for r in rows]

    async def get(self, discovery_id: str) -> Optional[dict]:
        row = await self._pool.fetchrow(
            f"SELECT {DISCOVERY_COLS} FROM agent_discoveries WHERE id = $1", discovery_id)
        return dict(row) if row is not None else None

    async def link(self, discovery_id: str, connected_agent_id: Optional[str]) -> None:
        await self._pool.execute(
            "UPDATE agent_discoveries SET connected_agent_id = $2 WHERE id = $1",
            discovery_id, connected_agent_id)


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
