from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import redis.asyncio as aioredis
from pydantic import BaseModel, Field

# --- models ---------------------------------------------------------------


class DecisionSignal(BaseModel):
    """A single ephemeral signal carried on the decision bus."""

    id: str
    kind: str
    team_id: Optional[str] = None
    task_id: Optional[str] = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class Proposal(BaseModel):
    """A vote proposal resolved by quorum consensus on the bus (ephemeral)."""

    id: str
    team_id: str
    question: str
    quorum: int
    task_id: Optional[str] = None
    status: str  # pending | approved | rejected
    approve_count: int = 0
    reject_count: int = 0
    created_at: datetime
    decided_at: Optional[datetime] = None


# --- storage keys ---------------------------------------------------------

SIGNALS_KEY = "daash:bus:signals"
PROPOSAL_PREFIX = "daash:proposal:"
# Vote tallies live OUTSIDE the proposal prefix so list_proposals' SCAN never
# mistakes a (set-typed) tally for a (hash-typed) proposal.
VOTES_PREFIX = "daash:votes:"

# One atomic vote. Duplicate voters are rejected, and the first side to reach
# quorum resolves the proposal exactly once even under a burst of concurrent
# votes (Redis runs scripts serially). Quorum is read from the hash so a
# resolved flag can never be set twice.
_VOTE_LUA = """
local prop = KEYS[1]
if redis.call('EXISTS', prop) == 0 then return 'notfound' end
if redis.call('HGET', prop, 'status') ~= 'pending' then return 'resolved' end
local approve_set = KEYS[2]
local reject_set = KEYS[3]
local voter = ARGV[1]
if redis.call('SISMEMBER', approve_set, voter) == 1
   or redis.call('SISMEMBER', reject_set, voter) == 1 then return 'duplicate' end
local target = (ARGV[2] == 'approve') and approve_set or reject_set
redis.call('SADD', target, voter)
redis.call('EXPIRE', target, ARGV[4])
if redis.call('SCARD', target) >= tonumber(redis.call('HGET', prop, 'quorum')) then
  local status = (ARGV[2] == 'approve') and 'approved' or 'rejected'
  redis.call('HSET', prop, 'status', status, 'decided_at', ARGV[3])
  return 'resolved:' .. status
end
return 'voted'
"""


class ProposalError(Exception):
    """Raised for proposal vote failures; `code` maps to an HTTP status."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code  # notfound | resolved | duplicate


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DecisionBus:
    """Redis-backed decision bus: rapid ephemeral signals + quorum voting.

    Deliberately holds no durable state — every key carries a TTL, so the bus
    self-heals and PostgreSQL remains the definitive source of record (PLAN
    risk mitigation for "Redis is ephemeral").
    """

    def __init__(self, redis_url: str, ttl_seconds: int = 3600, signals_max: int = 200):
        self._redis: aioredis.Redis = aioredis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl_seconds
        self._signals_max = signals_max
        self._vote_script = self._redis.register_script(_VOTE_LUA)

    async def close(self) -> None:
        await self._redis.aclose()

    # -- signals --

    async def publish(
        self,
        kind: str,
        team_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> DecisionSignal:
        signal = DecisionSignal(
            id=str(uuid.uuid4()),
            kind=kind,
            team_id=team_id,
            task_id=task_id,
            payload=payload or {},
            created_at=datetime.now(timezone.utc),
        )
        pipe = self._redis.pipeline()
        pipe.lpush(SIGNALS_KEY, signal.model_dump_json())
        pipe.ltrim(SIGNALS_KEY, 0, self._signals_max - 1)
        pipe.expire(SIGNALS_KEY, self._ttl)
        await pipe.execute()
        return signal

    async def recent_signals(self, limit: int = 50) -> list[DecisionSignal]:
        raw = await self._redis.lrange(SIGNALS_KEY, 0, limit - 1)
        return [DecisionSignal.model_validate_json(item) for item in raw]

    # -- proposals / voting --

    async def create_proposal(
        self, team_id: str, question: str, quorum: int = 1, task_id: str | None = None
    ) -> Proposal:
        proposal = Proposal(
            id=str(uuid.uuid4()),
            team_id=team_id,
            question=question,
            quorum=quorum,
            task_id=task_id,
            status="pending",
            created_at=datetime.now(timezone.utc),
        )
        key = self._proposal_key(proposal.id)
        pipe = self._redis.pipeline()
        pipe.hset(key, mapping=self._to_hash(proposal))
        pipe.expire(key, self._ttl)
        await pipe.execute()
        return proposal

    async def cast_vote(self, proposal_id: str, voter: str, approve: bool) -> Proposal:
        result = await self._vote_script(
            keys=[
                self._proposal_key(proposal_id),
                self._votes_key(proposal_id, "approve"),
                self._votes_key(proposal_id, "reject"),
            ],
            args=[voter, "approve" if approve else "reject", _now_iso(), str(self._ttl)],
        )
        decision = result.decode() if isinstance(result, bytes) else str(result)
        if decision == "notfound":
            raise ProposalError("notfound", f"proposal {proposal_id} not found")
        if decision == "resolved":
            raise ProposalError("resolved", f"proposal {proposal_id} is already decided")
        if decision == "duplicate":
            raise ProposalError("duplicate", f"voter {voter} has already voted on {proposal_id}")
        proposal = await self.get_proposal(proposal_id)
        assert proposal is not None
        return proposal

    async def get_proposal(self, proposal_id: str) -> Optional[Proposal]:
        data = await self._redis.hgetall(self._proposal_key(proposal_id))
        if not data:
            return None
        pipe = self._redis.pipeline()
        pipe.scard(self._votes_key(proposal_id, "approve"))
        pipe.scard(self._votes_key(proposal_id, "reject"))
        approves, rejects = await pipe.execute()
        return self._from_hash(data, approve_count=approves, reject_count=rejects)

    async def delete_proposal(self, proposal_id: str) -> None:
        """Test/ops helper: drop a proposal and its tallies immediately."""
        await self._redis.delete(
            self._proposal_key(proposal_id),
            self._votes_key(proposal_id, "approve"),
            self._votes_key(proposal_id, "reject"),
        )

    async def list_proposals(self, team_id: str | None = None) -> list[Proposal]:
        proposals: list[Proposal] = []
        # type="hash": skip stray keys under the prefix (e.g. legacy vote sets).
        async for key in self._redis.scan_iter(match=f"{PROPOSAL_PREFIX}*", count=100):
            if await self._redis.type(key) != "hash":
                continue
            data = await self._redis.hgetall(key)
            if not data:
                continue
            pipe = self._redis.pipeline()
            pipe.scard(self._votes_key(data["id"], "approve"))
            pipe.scard(self._votes_key(data["id"], "reject"))
            approves, rejects = await pipe.execute()
            proposal = self._from_hash(data, approve_count=approves, reject_count=rejects)
            if team_id is None or proposal.team_id == team_id:
                proposals.append(proposal)
        proposals.sort(key=lambda p: p.created_at, reverse=True)
        return proposals

    # -- helpers --

    @staticmethod
    def _proposal_key(pid: str) -> str:
        return f"{PROPOSAL_PREFIX}{pid}"

    @staticmethod
    def _votes_key(pid: str, choice: str) -> str:
        return f"{VOTES_PREFIX}{pid}:{choice}"

    @staticmethod
    def _to_hash(p: Proposal) -> dict[str, str]:
        return {
            "id": p.id,
            "team_id": p.team_id,
            "question": p.question,
            "quorum": str(p.quorum),
            "task_id": p.task_id or "",
            "status": p.status,
            "created_at": p.created_at.isoformat(),
            "decided_at": p.decided_at.isoformat() if p.decided_at else "",
        }

    @staticmethod
    def _from_hash(data: dict[str, str], *, approve_count: int = 0, reject_count: int = 0) -> Proposal:
        return Proposal(
            id=data["id"],
            team_id=data["team_id"],
            question=data["question"],
            quorum=int(data["quorum"]),
            task_id=data["task_id"] or None,
            status=data["status"],
            approve_count=approve_count,
            reject_count=reject_count,
            created_at=datetime.fromisoformat(data["created_at"]),
            decided_at=datetime.fromisoformat(data["decided_at"]) if data.get("decided_at") else None,
        )
