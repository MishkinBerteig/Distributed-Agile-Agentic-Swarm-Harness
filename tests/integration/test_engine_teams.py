"""Team proposals (vote, expire, materialize) and delivery agents working side by side
(docs/DESIGN-0.2.md A.2), with the engine in-process against the fake LLM."""
from __future__ import annotations

from app import policy
from app.models import ActionKind
from tests.integration.helpers import swarm_config

VISION = "Write a short guide to compost."


async def seeded(h, delivery=1, **limits):
    await h.create_swarm(VISION, config=swarm_config(delivery, **limits), dod="- The guide exists")
    team = await h.seed_team("Team A")
    await h.transition("start")
    return team


async def propose(h, team, name="Spin-off", voters=3):
    """What the propose_team tool does: a proposal plus the proposer's own yes vote."""
    agents = [a for a in await h.agent_list(team) if a.role == "mission_delivery"]
    assert len(agents) == voters
    prop = await h.proposals.create(
        team.swarm_id, team.id, agents[0].id, name,
        f"To advance the Vision, {name} owns a distinct part of it.", "Better as its own team",
        policy.majority_quorum(voters))
    return await h.proposals.record_vote(prop.id, agents[0].id, True)


async def proposal(h, pid):
    return await h.proposals.get(pid)


async def test_a_majority_vote_creates_a_team_which_then_does_its_own_work(h, fake):
    fake.configure({"tasks_per_plan": 1, "vote_approve": True})
    team = await seeded(h, delivery=3)
    prop = await propose(h, team)
    assert prop.status == "pending" and prop.quorum == 2

    await h.run_until(lambda: _decided(h, prop.id), label="vote decided")
    decided = await proposal(h, prop.id)
    assert decided.status == "approved" and decided.created_team_id is None
    assert len(fake.calls_for("vote")) == 1                         # proposer voted by proposing
    assert fake.calls_for("vote")[0]["role"] == "Mission Delivery"

    await h.step()                                                  # MATERIALIZE_TEAM (inline)
    decided = await proposal(h, prop.id)
    new = await h.teams.get(decided.created_team_id)
    assert new.name == "Spin-off" and new.creator_kind == "team" and new.creator_team_id == team.id
    assert sorted(a.role for a in await h.agent_list(new)) == [
        "mission_delivery"] * 3 + ["mission_judgement", "quality_judgement"]
    assert any("by majority vote of another team" in e.detail for e in await h.event_list("team"))

    await h.run_to_state("VERIFICATION")                            # both teams finish
    assert {t.status for t in await h.team_list()} == {"accomplished"}
    assert {t.name for t in await h.task_list()} == {"Team A task 1", "Spin-off task 1"}


async def _decided(h, pid):
    return (await proposal(h, pid)).status != "pending"


async def test_a_losing_vote_creates_no_team(h, fake):
    fake.configure({"vote_approve": False, "tasks_per_plan": 1})
    team = await seeded(h, delivery=3)
    prop = await propose(h, team)
    await h.run_until(lambda: _decided(h, prop.id), label="vote decided")
    decided = await proposal(h, prop.id)
    assert decided.status == "rejected" and decided.created_team_id is None
    assert sorted(v for v in decided.votes.values()) == [False, False, True]
    assert len(fake.calls_for("vote")) == 2
    await h.run_to_state("VERIFICATION")
    assert [t.name for t in await h.team_list()] == ["Team A"]
    assert any("rejected" in e.detail or "against" in e.detail
               for e in await h.event_list("proposal"))


async def test_a_stale_proposal_expires_without_a_vote(h, fake):
    team = await seeded(h, delivery=3)
    prop = await propose(h, team)
    await h.pool.execute("UPDATE team_proposals SET created_at = now() - interval '31 minutes' "
                         "WHERE id = $1", prop.id)
    actions = await h.step()
    assert ActionKind.EXPIRE_PROPOSAL in [a.kind for a in actions]
    assert ActionKind.VOTE not in [a.kind for a in actions]
    assert (await proposal(h, prop.id)).status == "rejected"
    assert fake.calls_for("vote") == []
    assert any("no majority within 30 minutes" in e.detail for e in await h.event_list("proposal"))
    assert len(await h.team_list()) == 1


async def test_a_proposal_from_a_team_that_is_no_longer_active_expires(h, fake):
    team = await seeded(h, delivery=3)
    prop = await propose(h, team)
    await h.teams.update(team.id, status="failed")
    await h.step()
    assert (await proposal(h, prop.id)).status == "rejected"
    assert any("no longer active" in e.detail for e in await h.event_list("proposal"))


async def test_propose_team_tool_with_one_delivery_agent_materializes_immediately(h, fake):
    """A lone delivery agent is the whole electorate: its own proposal carries the vote."""
    fake.configure({"work_propose_team": "Spin-off", "tasks_per_plan": 1})
    await h.create_swarm(VISION, config=swarm_config(), start=True)
    await h.run_to_state("VERIFICATION")
    teams = await h.team_list()
    assert sorted(t.name for t in teams) == ["Spin-off", "Team A"]
    (prop,) = await h.proposals.list(h.swarm_id)
    spin = next(t for t in teams if t.name == "Spin-off")
    assert prop.status == "approved" and prop.created_team_id == spin.id
    assert spin.creator_kind == "team" and spin.status == "accomplished"
    assert any("proposed a new team 'Spin-off'" in e.detail for e in await h.event_list("proposal"))
    assert fake.calls_for("vote") == []                             # nobody else to ask


async def test_propose_team_tool_refuses_when_the_swarm_is_at_its_team_limit(h, fake):
    fake.configure({"work_propose_team": "Spin-off", "tasks_per_plan": 1})
    await h.create_swarm(VISION, config=swarm_config(max_teams=1), start=True)
    await h.run_to_state("VERIFICATION")
    assert [t.name for t in await h.team_list()] == ["Team A"]
    assert await h.proposals.list(h.swarm_id) == []                 # the tool said no
    (task,) = await h.task_list()
    assert task.status == "Done"                                    # and the work went on


async def test_an_approved_proposal_is_rejected_when_the_team_limit_was_reached_meanwhile(h, fake):
    team = await seeded(h, delivery=1, max_teams=1)
    prop = await propose(h, team, voters=1)
    await h.proposals.set_status(prop.id, "approved")
    await h.step()
    assert (await proposal(h, prop.id)).status == "rejected"
    assert [t.name for t in await h.team_list()] == ["Team A"]
    assert any("maximum of 1 teams" in e.detail for e in await h.event_list("proposal"))


async def test_materialize_is_idempotent_after_a_crash(h, fake):
    """The team exists but the proposal was not yet marked: re-running does not duplicate."""
    team = await seeded(h, delivery=1)
    prop = await propose(h, team, voters=1)
    await h.proposals.set_status(prop.id, "approved")
    from app import services
    swarm = await h.swarm()
    existing = await services.create_team(h.pool, swarm, "Spin-off", "m",
                                          creator_kind="team", creator_team_id=team.id)
    await h.step()
    assert (await proposal(h, prop.id)).created_team_id == existing.id
    assert [t.name for t in await h.team_list()].count("Spin-off") == 1


# -- delivery agents side by side -------------------------------------------------------------

async def test_two_delivery_agents_work_different_tasks_at_the_same_time(h, fake):
    fake.configure({"tasks_per_plan": 4, "delay_ms": 120})
    team = await seeded(h, delivery=2)
    most = {"in_progress": 0, "busy": 0}

    async def watch():
        by = await h.by_status(team)
        most["in_progress"] = max(most["in_progress"], len(by.get("In-Progress", [])))
        busy = [a for a in await h.agent_list(team)
                if a.role == "mission_delivery" and a.state == "busy"]
        most["busy"] = max(most["busy"], len(busy))
        return (await h.state()) == "VERIFICATION"

    await h.run_free(watch, label="verification", timeout=60)
    tasks = await h.task_list()
    assert len(tasks) == 4 and {t.status for t in tasks} == {"Done"}
    assert len({t.assigned_agent_id for t in tasks}) == 2           # both agents delivered
    assert most["in_progress"] == 2 and most["busy"] == 2
    assert fake.max_overlap("work") >= 2                            # two agents' calls overlapped
    assert all(t.attempts == 1 for t in tasks)
    assert all(a.state == "idle" for a in await h.agent_list())


async def test_agents_never_work_the_same_task_twice(h, fake):
    fake.configure({"tasks_per_plan": 3, "delay_ms": 60})
    team = await seeded(h, delivery=3)
    await h.run_free(lambda: _verified(h), label="verification", timeout=60)
    works: dict[str, int] = {}
    for c in fake.calls_for("work"):
        if c["task_id"] and c["has_tools"]:
            works[c["task_id"]] = works.get(c["task_id"], 0) + 1
    assert set(works.values()) == {2}                               # write_file + submit, once
    assert len(await h.event_list("submitted")) == 3


async def _verified(h):
    return (await h.state()) == "VERIFICATION"
