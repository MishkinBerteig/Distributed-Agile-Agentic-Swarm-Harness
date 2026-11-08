"""Every repository method against real PostgreSQL, plus load_snapshot (docs/DESIGN-0.2.md A.2)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.embeddings import build_embedder
from app.models import (CheckKind, CriterionCreate, GlobalConfig, SwarmConfig, TaskCreate,
                        TaskStatus)
from app.repo import (AgentRepo, CallRepo, ConfigRepo, EventRepo, FeedbackRepo, LearningRepo,
                      MemoryRepo, ProposalRepo, SwarmRepo, TaskRepo, TeamRepo, VerdictRepo,
                      load_snapshot)

NOW = datetime.now(timezone.utc)
EMB = build_embedder("none", False)


@pytest.fixture
async def swarm(pool):
    return await SwarmRepo(pool).create("S", "The vision", None, SwarmConfig())


@pytest.fixture
async def team(pool, swarm):
    return await TeamRepo(pool).create(swarm.id, "Team A", "Mission A")


@pytest.fixture
async def agent(pool, swarm, team):
    return await AgentRepo(pool).create(swarm.id, team.id, "mission_delivery", "Delivery-1", "fast")


def tcreate(team, name="T1", **kw):
    return TaskCreate(team_id=team.id, name=name, acceptance_criteria=[
        CriterionCreate(text="exists", check_kind=CheckKind.FILE_EXISTS, check_path="a.md"),
        CriterionCreate(text="nice")], **kw)


# -- config ----------------------------------------------------------------------------

async def test_config_defaults_and_roundtrip(pool):
    repo = ConfigRepo(pool)
    cfg = await repo.get_global()
    assert cfg.values_principles == "" and cfg.default_swarm_config.limits.max_teams == 4
    new = GlobalConfig(values_principles="Be kind")
    new.default_swarm_config.limits.max_teams = 7
    await repo.put_global(new)
    await repo.put_global(new)                              # upsert, not insert
    got = await repo.get_global()
    assert got.values_principles == "Be kind" and got.default_swarm_config.limits.max_teams == 7


# -- swarms ----------------------------------------------------------------------------

async def test_swarm_crud_and_single_live_swarm(pool, swarm):
    repo = SwarmRepo(pool)
    assert swarm.lifecycle_state == "CREATED" and swarm.config.limits.max_teams == 4
    assert (await repo.get(swarm.id)).name == "S"
    assert await repo.get("nope") is None
    assert [s.id for s in await repo.list()] == [swarm.id]
    assert (await repo.get_live()).id == swarm.id
    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.create("S2", "v", None, SwarmConfig())
    cfg = SwarmConfig()
    cfg.limits.max_teams = 2
    upd = await repo.update(swarm.id, name="S!", lifecycle_state="ACTIVE", config=cfg,
                            started_at=NOW, paused_reason="why", retrospective_done=True,
                            definition_of_done="dod")
    assert (upd.name, upd.lifecycle_state, upd.config.limits.max_teams) == ("S!", "ACTIVE", 2)
    assert upd.started_at and upd.paused_reason == "why" and upd.retrospective_done
    with pytest.raises(ValueError):
        await repo.update(swarm.id, bogus=1)
    assert await repo.update("nope", name="x") is None
    await repo.update(swarm.id, lifecycle_state="ARCHIVED")
    assert await repo.get_live() is None
    assert await repo.delete(swarm.id) is True
    assert await repo.delete(swarm.id) is False


# -- teams / agents ------------------------------------------------------------------------

async def test_team_crud(pool, swarm, team):
    repo = TeamRepo(pool)
    child = await repo.create(swarm.id, "Spin", "m", creator_kind="team", creator_team_id=team.id)
    assert child.creator_kind == "team" and child.creator_team_id == team.id
    assert [t.name for t in await repo.list(swarm.id)] == ["Team A", "Spin"]
    upd = await repo.update(team.id, status="accomplished", definition_of_done="d",
                            definition_of_ready="r", generation=2, next_focus="f",
                            last_assessed_at=NOW)
    assert upd.status == "accomplished" and upd.generation == 2 and upd.last_assessed_at
    assert (await repo.get(team.id)).next_focus == "f"
    assert await repo.delete(team.id) is True
    assert (await repo.get(child.id)).creator_team_id is None        # ON DELETE SET NULL
    assert await repo.get(team.id) is None


async def test_agent_repo(pool, swarm, team, agent):
    repo = AgentRepo(pool)
    coord = await repo.create(swarm.id, None, "swarm_coordinator", "Swarm Coordinator", "amazing")
    assert coord.team_id is None and agent.state == "idle"
    assert {a.name for a in await repo.list(swarm.id)} == {"Swarm Coordinator", "Delivery-1"}
    assert [a.name for a in await repo.list(swarm.id, team.id)] == ["Delivery-1"]
    busy = await repo.set_state(agent.id, "busy", "task-x")
    assert busy.state == "busy" and busy.current_task_id == "task-x"
    await repo.record_usage(agent.id, 120, True)
    await repo.record_usage(agent.id, 30, False)
    got = await repo.get(agent.id)
    assert (got.tokens_used, got.calls, got.errors) == (150, 2, 1) and got.last_active_at
    assert await repo.reset_busy() == 1
    reset = await repo.get(agent.id)
    assert reset.state == "idle" and reset.current_task_id is None
    assert await repo.reset_busy() == 0
    assert await repo.get("nope") is None


# -- tasks -------------------------------------------------------------------------------

async def test_task_repo(pool, swarm, team):
    repo = TaskRepo(pool)
    emb = EMB.embed("hello")
    t1 = await repo.create(swarm.id, tcreate(team, "T1", artifact="a.md"), embedding=emb)
    t2 = await repo.create(swarm.id, tcreate(team, "T2", depends_on=[t1.id]), created_by="agent-1")
    assert t1.status == "Pending" and t1.created_by == "user" and t2.created_by == "agent-1"
    assert [c.ordinal for c in t1.criteria] == [1, 2]
    assert t1.criteria[0].check_kind == "file_exists" and t1.criteria[1].check_kind == "rubric"
    assert t2.depends_on == [t1.id]
    assert [t.name for t in await repo.list(swarm_id=swarm.id)] == ["T1", "T2"]
    assert [t.name for t in await repo.list(team_id=team.id, status="Pending")] == ["T1", "T2"]
    assert await repo.list(status="Done") == []
    assert len(await repo.list()) == 2
    child = await repo.create(swarm.id, tcreate(team, "Sub", parent_id=t1.id))
    assert [c.id for c in await repo.children(t1.id)] == [child.id]

    upd = await repo.update(t1.id, status=TaskStatus.READY, priority=1, complexity="simple",
                            attempts=2, feedback="fb", rejection_reason="r", approved_at=NOW,
                            deliverable={"summary": "s", "files": ["a.md"]}, name="T1b",
                            description="d", artifact="b.md", assigned_agent_id=None,
                            committed_at=NOW, done_at=NOW)
    assert (upd.status, upd.priority, upd.complexity, upd.attempts) == ("Ready", 1, "simple", 2)
    assert upd.deliverable == {"summary": "s", "files": ["a.md"]} and upd.updated_at >= t1.updated_at
    assert (upd.name, upd.artifact, upd.criteria[0].text) == ("T1b", "b.md", "exists")
    assert await repo.update("nope", name="x") is None
    with pytest.raises(ValueError):
        await repo.update(t1.id, embedding=[1])

    crit = await repo.update_criterion(t1.criteria[0].id, "green", 1.0, "ok")
    assert (crit.status, crit.score, crit.evidence) == ("green", 1.0, "ok") and crit.updated_at
    assert (await repo.get(t1.id)).criteria[0].status == "green"
    assert await repo.update_criterion("nope", "red", 0.0, "") is None

    assert await repo.delete(t1.id) is True                  # cascades criteria and children
    assert await repo.get(t1.id) is None and await repo.get(child.id) is None
    assert await repo.delete(t1.id) is False
    assert await pool.fetchval("SELECT count(*) FROM acceptance_criteria WHERE task_id = $1",
                               t1.id) == 0


# -- verdicts / events / calls ------------------------------------------------------------------

async def test_verdict_repo(pool, swarm, team, agent):
    task = await TaskRepo(pool).create(swarm.id, tcreate(team))
    repo = VerdictRepo(pool)
    v1 = await repo.add(task.id, 1, "quality_judgement", agent.id, False, 0.6, "bad",
                        {"criteria": {"2": {"score": 0.6}}})
    v2 = await repo.add(task.id, 1, "mission_judgement", None, True, 0.995)
    got = await repo.list_for_task(task.id)
    assert [v.id for v in got] == [v1.id, v2.id]
    assert got[0].details["criteria"]["2"]["score"] == 0.6 and got[1].reasoning == ""
    assert (got[0].passed, got[1].passed, got[1].agent_id) == (False, True, None)


async def test_event_repo(pool, swarm, team):
    repo = EventRepo(pool)
    t = await TaskRepo(pool).create(swarm.id, tcreate(team))
    e1 = await repo.add(swarm.id, "created", "first", team_id=team.id, task_id=t.id,
                        to_status="Pending", actor="user")
    e2 = await repo.add(swarm.id, "swarm", "second")
    e3 = await repo.add(swarm.id, "done", "third", team_id=team.id, from_status="Review",
                        to_status="Done")
    assert [e.id for e in await repo.list(swarm.id)] == [e3.id, e2.id, e1.id]    # newest first
    assert [e.id for e in await repo.list(swarm.id, limit=1)] == [e3.id]
    assert {e.id for e in await repo.list(swarm.id, team_id=team.id)} == {e1.id, e3.id}
    assert [e.id for e in await repo.list(swarm.id, task_id=t.id)] == [e1.id]
    assert e1.actor == "user" and e2.actor == "engine" and e3.from_status == "Review"
    since = await repo.list_since(swarm.id, None)
    assert [e.id for e in since] == [e1.id, e2.id, e3.id]                        # oldest first
    assert [e.id for e in await repo.list_since(swarm.id, e3.created_at)] == [e3.id]
    assert await repo.list_since(swarm.id, NOW + timedelta(days=1)) == []
    await TaskRepo(pool).delete(t.id)                                            # history stays
    assert len(await repo.list(swarm.id)) == 3


async def test_call_repo(pool, swarm, team, agent):
    repo = CallRepo(pool)
    c1 = await repo.add(swarm.id, "mission_delivery", "fast", "work_task", team_id=team.id,
                        agent_id=agent.id, prompt_tokens=10, completion_tokens=5, total_tokens=15,
                        duration_ms=7, transcript=[{"role": "user", "content": "x"}])
    await repo.add(swarm.id, "mission_delivery", "great", "work_task", agent_id=agent.id,
                   total_tokens=100, ok=False, error="boom")
    await repo.add(swarm.id, "swarm_coordinator", "fast", "bootstrap", total_tokens=5)
    assert c1.transcript == [{"role": "user", "content": "x"}] and c1.ok
    mine = await repo.list_for_agent(agent.id)
    assert len(mine) == 2 and mine[0].created_at >= mine[1].created_at
    assert [c.id for c in await repo.list_for_agent(agent.id, limit=1)] == [mine[0].id]
    assert any(c.transcript for c in mine)                       # with transcripts
    every = await repo.list_for_swarm(swarm.id)
    assert len(every) == 3 and all(c.transcript == [] for c in every)   # without
    assert await repo.tokens_by_model(swarm.id) == {"fast": 20, "great": 100}
    assert await repo.tokens_used(swarm.id) == 120
    assert await repo.tokens_used("nope") == 0
    bad = [c for c in every if not c.ok][0]
    assert bad.error == "boom"


# -- learnings / memories --------------------------------------------------------------------

async def test_learning_repo(pool, swarm, team):
    repo = LearningRepo(pool)
    mine = await repo.add("local lesson", "pattern", swarm_id=swarm.id, team_id=team.id,
                          embedding=EMB.embed("compost heap"))
    glob = await repo.add("global lesson", "routing", swarm_id=None,
                          embedding=EMB.embed("gateway models"))
    await repo.add("no embedding", "automation", swarm_id=swarm.id)
    assert mine.swarm_id == swarm.id and glob.swarm_id is None
    both = await repo.list(swarm.id)
    assert {x.content for x in both} == {"local lesson", "global lesson", "no embedding"}
    assert {x.content for x in await repo.list(swarm.id, include_global=False)} == {
        "local lesson", "no embedding"}
    assert len(await repo.list(swarm.id, limit=1)) == 1
    hits = await repo.search(EMB.embed("compost heap"), swarm.id, k=1)
    assert hits[0].content == "local lesson"
    assert len(await repo.search(None, swarm.id, k=2)) == 2           # recency fallback
    assert {x.content for x in await repo.search(EMB.embed("x"), swarm.id,
                                                 include_global=False, k=8)} == {
        "local lesson", "no embedding"}


async def test_memory_repo(pool, swarm, team, agent):
    repo = MemoryRepo(pool)
    other = await AgentRepo(pool).create(swarm.id, team.id, "mission_delivery", "Delivery-2", "fast")
    await repo.add(agent.id, swarm.id, "remember compost", team_id=team.id,
                   embedding=EMB.embed("remember compost"))
    await repo.add(agent.id, swarm.id, "other note", embedding=EMB.embed("other note"))
    await repo.add(agent.id, swarm.id, "plain")
    await repo.add(other.id, swarm.id, "not mine")
    assert {m.content for m in await repo.list(agent.id)} == {"remember compost", "other note",
                                                              "plain"}
    assert len(await repo.list(agent.id, limit=2)) == 2
    assert (await repo.search(agent.id, EMB.embed("remember compost"), k=1))[0].content == \
        "remember compost"
    assert len(await repo.search(agent.id, None, k=8)) == 3
    assert (await repo.list(other.id))[0].content == "not mine"


# -- proposals / feedback ---------------------------------------------------------------------

async def test_proposal_repo(pool, swarm, team, agent):
    repo = ProposalRepo(pool)
    p = await repo.create(swarm.id, team.id, agent.id, "Spin", "Mission", "because", 2)
    assert (p.status, p.quorum, p.votes) == ("pending", 2, {})
    assert (await repo.get(p.id)).name == "Spin" and await repo.get("nope") is None
    p = await repo.record_vote(p.id, "a1", True)
    p = await repo.record_vote(p.id, "a2", False)
    assert p.votes == {"a1": True, "a2": False}
    p = await repo.record_vote(p.id, "a2", True)                       # overwrite
    assert p.votes == {"a1": True, "a2": True}
    assert await repo.record_vote("nope", "a", True) is None
    q = await repo.create(swarm.id, team.id, agent.id, "Other", "M", "", 1)
    assert [x.name for x in await repo.list(swarm.id)] == ["Spin", "Other"]
    done = await repo.set_status(p.id, "approved", created_team_id=team.id)
    assert done.status == "approved" and done.created_team_id == team.id and done.decided_at
    kept = await repo.set_status(p.id, "approved")                     # COALESCE keeps the id
    assert kept.created_team_id == team.id
    assert [x.id for x in await repo.list(swarm.id, status="pending")] == [q.id]
    assert [x.id for x in await repo.list(swarm.id, status="approved")] == [p.id]


async def test_concurrent_votes_are_not_lost(pool, swarm, team, agent):
    import asyncio

    repo = ProposalRepo(pool)
    p = await repo.create(swarm.id, team.id, agent.id, "Spin", "M", "", 3)
    await asyncio.gather(*[repo.record_vote(p.id, f"a{i}", i % 2 == 0) for i in range(8)])
    assert len((await repo.get(p.id)).votes) == 8


async def test_feedback_repo(pool, swarm):
    repo = FeedbackRepo(pool)
    a = await repo.add(swarm.id, "first")
    b = await repo.add(swarm.id, "second")
    assert [f.id for f in await repo.list(swarm.id)] == [a.id, b.id]       # oldest first
    assert await repo.list("nope") == []


# -- snapshot ----------------------------------------------------------------------------------

async def test_load_snapshot(pool, swarm, team, agent):
    assert await load_snapshot(pool, "nope", NOW, set()) is None
    tasks = TaskRepo(pool)
    for i in range(6):                                  # six Done tasks -> p90 exists
        t = await tasks.create(swarm.id, tcreate(team, f"D{i}"))
        await tasks.update(t.id, status="Done", committed_at=NOW - timedelta(seconds=100 - i),
                           done_at=NOW)
    pending = await tasks.create(swarm.id, tcreate(team, "P"))
    await ProposalRepo(pool).create(swarm.id, team.id, agent.id, "Spin", "M", "", 1)
    cfg = SwarmConfig()
    cfg.model_prices = {"fast": 2.0}
    await SwarmRepo(pool).update(swarm.id, config=cfg)
    await CallRepo(pool).add(swarm.id, "mission_delivery", "fast", "work_task", total_tokens=500)
    snap = await load_snapshot(pool, swarm.id, NOW, {"work_task:x:y"})
    assert snap.swarm.id == swarm.id and [t.name for t in snap.teams] == ["Team A"]
    assert len(snap.tasks) == 7 and pending.id in {t.id for t in snap.tasks}
    assert all(t.criteria for t in snap.tasks)
    assert [p.name for p in snap.proposals] == ["Spin"] and len(snap.agents) == 1
    assert snap.p90_cycle_seconds is not None and 90 <= snap.p90_cycle_seconds <= 100
    assert snap.tokens_used == 500 and snap.cost_usd == pytest.approx(1.0)
    assert snap.now == NOW and snap.running_jobs == {"work_task:x:y"}


async def test_snapshot_p90_needs_five_done_tasks(pool, swarm, team):
    tasks = TaskRepo(pool)
    for i in range(4):
        t = await tasks.create(swarm.id, tcreate(team, f"D{i}"))
        await tasks.update(t.id, status="Done", committed_at=NOW - timedelta(seconds=10),
                           done_at=NOW)
    assert (await load_snapshot(pool, swarm.id, NOW, set())).p90_cycle_seconds is None
