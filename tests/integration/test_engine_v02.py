"""DESIGN-0.2 section B behaviour, written to the spec (marker `v02`).

Select with `-m v02`, skip with `-m "not v02"`. These pin: B.1 batch approval, B.2 judges do not
serialize, B.3 per-model concurrency, B.4 leaving ACTIVE cancels spending and verification
closes open tasks. Until section B is implemented most of them fail by design.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.models import ActionKind
from tests.integration.helpers import make_llm, swarm_config

pytestmark = pytest.mark.v02

VISION = "Write a short guide to compost."
CLOSED = "Closed: the swarm went to verification before this task finished."


async def seeded(h, delivery=1, **limits):
    await h.create_swarm(VISION, config=swarm_config(delivery, **limits), dod="- The guide exists")
    team = await h.seed_team("Team A")
    await h.transition("start")
    return team


async def delivery_agent(h, team=None):
    return next(a for a in await h.agent_list(team) if a.role == "mission_delivery")


def write_workspace(h, rel, text):
    ws = Path(h.settings.WORKSPACE_ROOT) / h.swarm_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / rel).write_text(text)


# -- B.1 batch approval ------------------------------------------------------------------------

async def test_one_prioritize_call_approves_as_many_tasks_as_there_are_delivery_agents(h, fake):
    fake.configure({"tasks_per_plan": 2})
    team = await seeded(h, delivery=2)
    for i in range(1, 5):
        await h.add_task(team, f"Task {i}")

    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.PRIORITIZE]
    by = await h.by_status(team)
    assert len(by["Ready"]) == 2 and len(by["Pending"]) == 2        # two slots -> two approvals
    assert sorted(t.priority for t in by["Ready"]) == [1, 2]
    assert len(fake.calls_for("prioritize")) == 1

    actions = await h.step()                                        # both agents commit at once
    assert [a.kind for a in actions].count(ActionKind.COMMIT_TASK) == 2
    by = await h.by_status(team)
    assert len(by["In-Progress"]) == 2
    assert len({t.assigned_agent_id for t in by["In-Progress"]}) == 2

    await h.step()                                                  # work, and the buffer refills
    by = await h.by_status(team)
    assert len(by["Review"]) == 2 and len(by["Ready"]) == 2

    await h.run_to_state("VERIFICATION")
    assert {t.status for t in await h.task_list()} == {"Done"}
    assert len(fake.calls_for("prioritize")) <= 4                   # not one call per task (6)


async def test_a_single_agent_team_still_approves_one_task_at_a_time(h, fake):
    team = await seeded(h, delivery=1)
    for i in range(1, 4):
        await h.add_task(team, f"Task {i}")
    await h.step()
    by = await h.by_status(team)
    assert len(by["Ready"]) == 1 and len(by["Pending"]) == 2


# -- B.2 judges do not serialize -----------------------------------------------------------------

async def test_the_judges_review_two_tasks_at_the_same_time(h, fake):
    fake.configure({"delay_ms": 250})
    team = await seeded(h)
    qj = next(a for a in await h.agent_list(team) if a.role == "quality_judgement")
    mj = next(a for a in await h.agent_list(team) if a.role == "mission_judgement")
    for n in (1, 2):
        art = f"r{n}.md"
        write_workspace(h, art, "# Overview\n" + "word " * 40)
        await h.add_task(team, f"Review {n}", artifact=art, status="Review",
                         deliverable={"summary": "done", "files": [art]})

    actions = await h.engine.tick()
    assert [a.kind for a in actions].count(ActionKind.REVIEW_TASK) == 2
    await h.wait_for(lambda: bool(fake.calls_for("review_quality")), label="a review in flight")
    assert (await h.agents.get(qj.id)).state == "busy"              # BUSY while a job runs
    await h.settle()

    assert fake.max_overlap("review_quality") == 2                  # one QJ, two reviews at once
    assert fake.max_overlap("review_mission") == 2
    assert {t.status for t in await h.task_list()} == {"Done"}
    assert (await h.agents.get(qj.id)).state == "idle"              # idle when the LAST ends
    assert (await h.agents.get(mj.id)).state == "idle"


# -- B.3 per-model concurrency -----------------------------------------------------------------------

async def test_llm_client_limits_concurrent_calls_per_model(fake):
    fake.configure({"delay_ms": 80})
    client = make_llm(fake, default_concurrency=3, model_concurrency={"amazing": 1})
    msgs = [{"role": "user", "content": "hello"}]
    await asyncio.gather(*[client.chat("amazing", msgs) for _ in range(4)])
    assert fake.max_overlap("unknown", model="amazing") == 1        # per-model limit
    await asyncio.gather(*[client.chat("fast", msgs) for _ in range(6)])
    assert fake.max_overlap("unknown", model="fast") == 3           # the default limit


async def test_concurrency_settings_default_to_four():
    assert Settings().LLM_MAX_CONCURRENCY == 4


# -- B.4 leaving ACTIVE stops spending -------------------------------------------------------------------

async def test_pause_cancels_a_running_work_job_and_resume_redoes_it(h, fake):
    fake.configure({"delay_ms": 300})
    team = await seeded(h)
    agent = await delivery_agent(h)
    task = await h.add_task(team, "Held task", status="In-Progress", assigned_agent_id=agent.id,
                            committed_at=datetime.now(timezone.utc), complexity="simple")
    await h.engine.tick()                                           # the work job starts
    await h.wait_for(lambda: len(fake.calls_for("work")) == 1, label="work call in flight")

    await h.transition("pause")
    await h.engine.tick()                                           # next tick: cancel
    await h.settle(timeout=5)
    assert not h.engine.running
    work = fake.calls_for("work")
    assert len(work) == 1 and work[0]["cancelled"] is True          # no further spending
    after = await h.tasks.get(task.id)
    assert after.status == "In-Progress" and after.deliverable == {} and after.attempts == 0
    assert not await h.event_list("submitted")
    assert (await h.agents.get(agent.id)).state == "idle"
    spent = len(fake.calls)
    assert await h.step() == [] and len(fake.calls) == spent        # still paused

    await h.transition("resume")                                    # decide() re-dispatches it
    await h.run_to_state("VERIFICATION")
    done = await h.tasks.get(task.id)
    assert done.status == "Done" and done.attempts == 1


async def test_pausing_while_a_plan_is_in_flight_discards_its_result(h, fake):
    fake.configure({"delay_ms": 300})
    await seeded(h)
    await h.engine.tick()                                           # PLAN_TASKS starts
    await h.wait_for(lambda: bool(fake.calls_for("plan")), label="plan call in flight")
    await h.transition("pause")
    await h.engine.tick()
    await h.settle(timeout=5)
    assert await h.task_list() == []                                # nothing written while paused
    fake.configure({"delay_ms": 0})
    await h.transition("resume")
    await h.run_to_state("VERIFICATION")
    assert {t.status for t in await h.task_list()} == {"Done"}


async def test_a_job_that_finishes_after_the_swarm_left_active_writes_nothing(h, fake):
    """Even if the job was not cancelled in time, a result is not stored once the swarm is
    no longer ACTIVE (here: moved to USER_FEEDBACK via pause + feedback)."""
    fake.configure({"delay_ms": 300})
    team = await seeded(h)
    for i in range(2):
        await h.add_task(team, f"Pending {i}")
    await h.engine.tick()                                           # PRIORITIZE starts
    await h.wait_for(lambda: bool(fake.calls_for("prioritize")), label="prioritize in flight")
    await h.transition("pause")
    await h.transition("feedback")
    await h.engine.tick()
    await h.settle(timeout=5)
    by = await h.by_status(team)
    assert "Ready" not in by and len(by["Pending"]) == 2


async def test_verify_closes_open_tasks_and_cancels_the_running_job(h, fake, api):
    fake.configure({"delay_ms": 300})
    team = await seeded(h)
    agent = await delivery_agent(h)
    task = await h.add_task(team, "Held task", status="In-Progress", assigned_agent_id=agent.id,
                            committed_at=datetime.now(timezone.utc), complexity="simple")
    done = await h.add_task(team, "Finished", status="Done", done_at=datetime.now(timezone.utc))
    pending = await h.add_task(team, "Still pending")
    await h.engine.tick()
    await h.wait_for(lambda: len(fake.calls_for("work")) == 1, label="work call in flight")

    r = await api.post(f"/swarms/{h.swarm_id}/transitions/verify")
    assert r.status_code == 200 and r.json()["lifecycle_state"] == "VERIFICATION"
    await h.engine.tick()
    await h.settle(timeout=5)

    for tid in (task.id, pending.id):
        t = await h.tasks.get(tid)
        assert t.status == "Rejected" and t.rejection_reason == CLOSED
    assert (await h.tasks.get(done.id)).status == "Done"
    assert len(fake.calls_for("work")) == 1                         # the in-flight job was cancelled
    assert not await h.event_list("submitted")
    assert (await h.agents.get(agent.id)).state == "idle"


@pytest.mark.parametrize("via_pause", [False, True])
async def test_user_verify_through_the_api_closes_every_open_task(api, pool, via_pause):
    from app.models import TaskCreate
    from app.repo import TaskRepo

    s = (await api.post("/swarms", json={"name": "S", "vision_statement": "v", "teams": [
        {"name": "Docs", "mission_statement": "m"}]})).json()
    tid = s["teams"][0]["id"]
    await api.post(f"/swarms/{s['id']}/transitions/start")
    repo = TaskRepo(pool)
    ids = {}
    for status in ("Pending", "Ready", "In-Progress", "Review", "Done", "Failed", "Rejected"):
        t = await repo.create(s["id"], TaskCreate(team_id=tid, name=status, acceptance_criteria=[
            {"text": "x"}]))
        await repo.update(t.id, status=status, rejection_reason="earlier reason"
                          if status == "Rejected" else None)
        ids[status] = t.id
    if via_pause:
        await api.post(f"/swarms/{s['id']}/transitions/pause")
    r = await api.post(f"/swarms/{s['id']}/transitions/verify")
    assert r.status_code == 200
    got = {st: await repo.get(i) for st, i in ids.items()}
    for st in ("Pending", "Ready", "In-Progress", "Review"):
        assert got[st].status == "Rejected" and got[st].rejection_reason == CLOSED, st
    assert got["Done"].status == "Done" and got["Failed"].status == "Failed"
    assert got["Rejected"].rejection_reason == "earlier reason"


async def test_completing_the_swarm_closes_tasks_that_could_never_start(h, fake):
    """COMPLETE_SWARM goes through the same closing function as the user's verify."""
    fake.configure({"tasks_per_plan": 1})
    team = await seeded(h)
    dead = await h.add_task(team, "Dead task", status="Failed", attempts=3)
    stuck = await h.add_task(team, "Behind the dead one", depends_on=[dead.id])
    await h.run_to_state("VERIFICATION")
    t = await h.tasks.get(stuck.id)
    assert t.status == "Rejected" and t.rejection_reason == CLOSED


async def test_the_retrospective_is_not_cancelled_in_learning(h, fake):
    """LEARNING is the one non-ACTIVE state where jobs keep running."""
    fake.configure({"tasks_per_plan": 1})
    await h.create_swarm(VISION, config=swarm_config(), start=True)
    await h.run_to_state("VERIFICATION")
    await h.transition("learn")
    fake.configure({"delay_ms": 200})
    await h.engine.tick()                                           # RETROSPECTIVE starts
    await h.wait_for(lambda: bool(fake.calls_for("retrospective")), label="retrospective call")
    await h.engine.tick()                                           # must not cancel it
    await h.settle(timeout=10)
    assert (await h.swarm()).retrospective_done is True
    assert fake.calls_for("retrospective")[0]["cancelled"] is False
