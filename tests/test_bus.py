"""Slice 2: Decision Bus — voting logic, consensus under parallel votes, ephemerality."""
from __future__ import annotations

import asyncio

import pytest

from app.bus import DecisionBus, ProposalError
from tests.conftest import TEST_REDIS_URL

pytestmark = pytest.mark.asyncio


# --- signal feed ---


async def test_publish_and_recent_signals_newest_first(bus: DecisionBus):
    first = await bus.publish("task_blocked", team_id="t1", payload={"reason": "api quota"})
    second = await bus.publish("veto", team_id="t1", task_id=str(first.id))

    signals = await bus.recent_signals()
    assert [s.id for s in signals] == [second.id, first.id]
    assert signals[0].kind == "veto" and signals[0].task_id == first.id
    assert signals[1].payload == {"reason": "api quota"}


async def test_signal_feed_is_capped(bus: DecisionBus):
    capped = DecisionBus(TEST_REDIS_URL, ttl_seconds=60, signals_max=5)
    try:
        for i in range(12):
            await capped.publish(f"signal-{i}")
        signals = await capped.recent_signals(limit=50)
        assert len(signals) == 5
        assert [s.kind for s in signals] == [f"signal-{i}" for i in range(11, 6, -1)]
    finally:
        await capped.close()


# --- voting logic (unit-level, per PLAN "Voting logic unit tests") ---


async def test_quorum_of_approves_resolves_approved(bus: DecisionBus):
    p = await bus.create_proposal("team-a", "ship the memory module?", quorum=2)
    assert p.status == "pending"

    after_first = await bus.cast_vote(p.id, voter="agent-1", approve=True)
    assert (after_first.status, after_first.approve_count) == ("pending", 1)

    after_second = await bus.cast_vote(p.id, voter="agent-2", approve=True)
    assert after_second.status == "approved"
    assert after_second.decided_at is not None


async def test_quorum_of_rejects_resolves_rejected(bus: DecisionBus):
    p = await bus.create_proposal("team-a", "drop the retry logic?", quorum=2)
    await bus.cast_vote(p.id, voter="agent-1", approve=False)
    final = await bus.cast_vote(p.id, voter="agent-2", approve=False)
    assert final.status == "rejected"
    assert (final.reject_count, final.approve_count) == (2, 0)


async def test_quorum_one_resolves_immediately(bus: DecisionBus):
    p = await bus.create_proposal("team-a", "solo swarm decision", quorum=1)
    final = await bus.cast_vote(p.id, voter="lone-agent", approve=True)
    assert final.status == "approved"


async def test_duplicate_vote_is_rejected(bus: DecisionBus):
    p = await bus.create_proposal("team-a", "duplicate?", quorum=2)
    await bus.cast_vote(p.id, voter="agent-1", approve=True)
    with pytest.raises(ProposalError) as exc:
        await bus.cast_vote(p.id, voter="agent-1", approve=False)
    assert exc.value.code == "duplicate"
    fresh = await bus.get_proposal(p.id)
    assert (fresh.status, fresh.approve_count, fresh.reject_count) == ("pending", 1, 0)


async def test_vote_on_resolved_proposal_is_rejected(bus: DecisionBus):
    p = await bus.create_proposal("team-a", "already decided?", quorum=1)
    await bus.cast_vote(p.id, voter="agent-1", approve=True)
    with pytest.raises(ProposalError) as exc:
        await bus.cast_vote(p.id, voter="agent-2", approve=False)
    assert exc.value.code == "resolved"
    fresh = await bus.get_proposal(p.id)
    assert (fresh.status, fresh.approve_count) == ("approved", 1)


async def test_vote_on_unknown_proposal(bus: DecisionBus):
    with pytest.raises(ProposalError) as exc:
        await bus.cast_vote("00000000-0000-0000-0000-000000000000", voter="agent-1", approve=True)
    assert exc.value.code == "notfound"


async def test_list_and_team_filter_proposals(bus: DecisionBus):
    p1 = await bus.create_proposal("team-a", "a?")
    p2 = await bus.create_proposal("team-b", "b?")

    everything = await bus.list_proposals()
    assert {p.id for p in everything} == {p1.id, p2.id}
    only_b = await bus.list_proposals(team_id="team-b")
    assert [p.id for p in only_b] == [p2.id]


# --- consensus under parallel votes (PLAN "Consensus reached under parallel votes") ---


async def test_consensus_reached_under_parallel_votes(bus: DecisionBus):
    """A 3-agent swarm races all votes at once; the quorum must resolve exactly once."""
    p = await bus.create_proposal("swarm-3", "consensus under concurrency?", quorum=2)

    async def vote(voter: str):
        try:
            return await bus.cast_vote(p.id, voter=voter, approve=True)
        except ProposalError as exc:
            # Votes that land after quorum are refused — that's the point.
            assert exc.code == "resolved"
            return None

    results = await asyncio.gather(*(vote(f"agent-{i}") for i in range(1, 4)))

    final = await bus.get_proposal(p.id)
    assert final.status == "approved"
    assert final.approve_count >= 2
    # Exactly one call observed the transition to resolved.
    assert sum(r is not None and r.status == "approved" for r in results) == 1


async def test_first_side_to_reach_quorum_wins_under_race(bus: DecisionBus):
    """Mixed approve/reject race: whichever side hits quorum first decides it, once."""
    p = await bus.create_proposal("swarm-4", "split vote race?", quorum=2)

    async def vote(voter: str, approve: bool):
        try:
            return await bus.cast_vote(p.id, voter=voter, approve=approve)
        except ProposalError as exc:
            assert exc.code == "resolved"
            return None

    await asyncio.gather(
        vote("agent-1", True),
        vote("agent-2", False),
        vote("agent-3", True),
        vote("agent-4", False),
    )

    final = await bus.get_proposal(p.id)
    assert final.status in ("approved", "rejected")
    winner = final.approve_count if final.status == "approved" else final.reject_count
    loser = final.reject_count if final.status == "approved" else final.approve_count
    assert winner >= 2 and winner <= 3 and loser < 2


async def test_parallel_duplicate_voters_count_once(bus: DecisionBus):
    p = await bus.create_proposal("swarm-3", "same voter racing itself?", quorum=2)
    results = await asyncio.gather(
        *(bus.cast_vote(p.id, voter="agent-1", approve=True) for _ in range(5)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ProposalError) and r.code == "duplicate" for r in results) == 4

    fresh = await bus.get_proposal(p.id)
    assert (fresh.status, fresh.approve_count) == ("pending", 1)


# --- ephemerality (PLAN: Redis is ephemeral — TTL self-healing) ---


async def test_list_proposals_skips_legacy_non_hash_keys(bus: DecisionBus):
    """Guard: stray non-hash keys under the proposal prefix (e.g. pre-fix vote
    tallies left in Redis) must not crash the listing with WRONGTYPE."""
    good = await bus.create_proposal("team-a", "list alongside junk?")
    # Exact key format the old code wrote: a SET inside daash:proposal:*.
    await bus._redis.sadd(f"daash:proposal:{good.id}:votes:approve", "old-voter")

    listed = await bus.list_proposals()
    assert [p.id for p in listed] == [good.id]


async def test_list_proposals_survives_existing_votes(bus: DecisionBus):
    """Regression: vote tallies (sets) must not break the proposal scan (hashes)."""
    p = await bus.create_proposal("team-a", "vote then list?", quorum=3)
    await bus.cast_vote(p.id, voter="agent-1", approve=True)
    await bus.cast_vote(p.id, voter="agent-2", approve=False)

    listed = await bus.list_proposals()
    assert [x.id for x in listed] == [p.id]
    assert (listed[0].approve_count, listed[0].reject_count) == (1, 1)


async def test_vote_tallies_expire_with_ttl(bus: DecisionBus):
    p = await bus.create_proposal("team-a", "tally ttl?")
    await bus.cast_vote(p.id, voter="agent-1", approve=True)

    approve_ttl = await bus._redis.ttl(bus._votes_key(p.id, "approve"))
    assert 0 < approve_ttl <= 60
    # A side that was never voted on has no key at all — nothing to expire.
    assert await bus._redis.exists(bus._votes_key(p.id, "reject")) == 0


async def test_bus_state_carries_ttl(bus: DecisionBus):
    p = await bus.create_proposal("team-a", "ttl please?")
    await bus.publish("quality_score", team_id="team-a")

    for key in (f"daash:proposal:{p.id}", "daash:bus:signals"):
        ttl = await bus._redis.ttl(key)
        assert 0 < ttl <= 60


async def test_bus_state_disappears_when_ttl_expires():
    short_lived = DecisionBus(TEST_REDIS_URL, ttl_seconds=1)
    try:
        p = await short_lived.create_proposal("team-a", "expire me")
        await short_lived.publish("fading")
        assert await short_lived.get_proposal(p.id) is not None

        # Force expiry deterministically rather than sleeping on wall-clock.
        await short_lived._redis.pexpire(f"daash:proposal:{p.id}", 0)
        await short_lived._redis.pexpire("daash:bus:signals", 0)

        assert await short_lived.get_proposal(p.id) is None
        assert await short_lived.recent_signals() == []
    finally:
        await short_lived.close()


# --- end-to-end through the HTTP API ---


async def test_end_to_end_consensus_flow(client):
    """E2E (PLAN): create swarm → proposal → agents vote in parallel → consensus."""
    swarm = await client.post("/swarms", json={"name": "Decision Swarm"})
    assert swarm.status_code == 201, swarm.text
    team_id = swarm.json()["id"]

    task = await client.post(
        "/tasks",
        json={"team_id": team_id, "name": "Wire the decision bus", "description": "Slice 2"},
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]

    signal = await client.post(
        "/decisions",
        json={"kind": "task_blocked", "team_id": team_id, "task_id": task_id,
              "payload": {"reason": "waiting on review"}},
    )
    assert signal.status_code == 201, signal.text

    feed = await client.get("/decisions")
    assert feed.status_code == 200
    assert feed.json()["signals"][0]["kind"] == "task_blocked"

    proposal = await client.post(
        "/proposals",
        json={"team_id": team_id, "question": "approve the wiring?", "quorum": 2,
              "task_id": task_id},
    )
    assert proposal.status_code == 201, proposal.text
    pid = proposal.json()["id"]
    assert proposal.json()["status"] == "pending"

    votes = await asyncio.gather(
        *(client.post(f"/proposals/{pid}/votes", json={"voter": f"agent-{i}", "approve": True})
          for i in range(1, 4)),
    )
    # Quorum=2 means 2 votes return 200 (one with transition, one pending),
    # the 3rd gets 409 because the proposal is already resolved.
    successes = [v for v in votes if v.status_code == 200]
    assert len(successes) == 2
    statuses = {v.json()["status"] for v in successes}
    assert statuses == {"approved", "pending"}

    final = await client.get(f"/proposals/{pid}")
    assert final.status_code == 200
    body = final.json()
    assert body["status"] == "approved"
    assert body["approve_count"] >= 2 and body["decided_at"] is not None

    # Late vote on a decided proposal conflicts.
    late = await client.post(f"/proposals/{pid}/votes", json={"voter": "agent-9", "approve": False})
    assert late.status_code == 409

    feed = await client.get("/decisions")
    kinds = [s["kind"] for s in feed.json()["signals"]]
    assert "proposal_approved" in kinds and "task_blocked" in kinds


async def test_vote_errors_map_to_http(client):
    unknown = await client.post(
        "/proposals/00000000-0000-0000-0000-000000000000/votes",
        json={"voter": "a", "approve": True},
    )
    assert unknown.status_code == 404

    proposal = await client.post("/proposals", json={"team_id": "t", "question": "?"})
    pid = proposal.json()["id"]
    first = await client.post(f"/proposals/{pid}/votes", json={"voter": "a", "approve": True})
    assert first.status_code == 200 and first.json()["status"] == "approved"

    repeat = await client.post(f"/proposals/{pid}/votes", json={"voter": "z", "approve": False})
    assert repeat.status_code == 409
