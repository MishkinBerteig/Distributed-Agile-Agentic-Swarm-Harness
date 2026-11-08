"""Engine resilience, in-process (docs/DESIGN-0.2.md A.2): restart semantics, user overrides
while jobs run, resource limits, gateway outages, parse-failure escalation, the re-ask path
for blocking verdicts, and a dead-dependency stall."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.engine import Cooldowns, Engine
from app.jobs import Jobs
from app.models import ActionKind
from tests.integration.helpers import swarm_config

VISION = "Write a short guide to compost."


async def seeded(h, delivery=1, **limits):
    """A started swarm with its vision DoD and one team with standards already in place, so
    the engine goes straight to planning. Returns the team."""
    await h.create_swarm(VISION, config=swarm_config(delivery, **limits),
                         dod="- The guide exists")
    team = await h.seed_team("Team A")
    await h.transition("start")
    return team


async def start_empty_swarm(h, **cfg):
    await h.create_swarm(VISION, config=swarm_config(**cfg))
    await h.transition("start")


async def delivery_agent(h):
    return next(a for a in await h.agent_list() if a.role == "mission_delivery")


# -- restart semantics --------------------------------------------------------------------

async def test_new_engine_mid_run_resets_busy_agents_and_resumes_without_duplicate_work(
        h, fake, settings, clock):
    fake.configure({"delay_ms": 120, "tasks_per_plan": 2})
    await start_empty_swarm(h)
    await h.run_until(lambda: _in_status(h, "In-Progress"), label="a task is In-Progress")
    task = (await h.by_status())["In-Progress"][0]

    # the work job is in flight when the process "dies": jobs are cancelled and the agent is
    # left BUSY in the database, as after a crash
    old = h.engine
    await old.tick()
    await h.wait_for(lambda: bool(fake.calls_for("work")), label="work call in flight")
    for job in list(old.running.values()):
        job.cancel()
    await h.settle(old)
    agent = await delivery_agent(h)
    await h.agents.set_state(agent.id, "busy", task.id)
    assert (await h.agents.get(agent.id)).state == "busy"

    # a brand-new Engine starts the way `python -m app.engine` does
    new = Engine(settings)
    await new.start()
    try:
        new.cooldowns = Cooldowns(clock=clock)
        new.jobs = Jobs(new.pool, h.llm, new.bus, h.embedder, settings)
        assert (await h.agents.get(agent.id)).state == "idle"              # reset_busy()
        again = await h.tasks.get(task.id)
        assert again.status == "In-Progress" and again.assigned_agent_id == agent.id

        await h.run_to_state("VERIFICATION", engine=new)
    finally:
        await new.shutdown()

    tasks = await h.task_list()
    assert len(tasks) == 2 and {t.status for t in tasks} == {"Done"}
    assert all(t.attempts == 1 for t in tasks)                              # no repeated attempt
    # nothing was done twice
    for job in ("bootstrap", "standards", "plan"):
        assert len(fake.calls_for(job)) == 1, job
    assert len([e for e in await h.event_list("committed")]) == 2
    assert len([e for e in await h.event_list("submitted")]) == 2
    for t in tasks:
        verdicts = await h.verdicts.list_for_task(t.id)
        assert sorted(v.judge_role for v in verdicts) == ["mission_judgement", "quality_judgement"]
    assert all(a.state == "idle" for a in await h.agent_list())


async def _in_status(h, status):
    return bool((await h.by_status()).get(status))


# -- user overrides while jobs run ----------------------------------------------------------

async def test_user_rejects_a_task_while_the_agent_is_working_on_it(h, fake):
    fake.configure({"delay_ms": 150, "tasks_per_plan": 2})
    await start_empty_swarm(h)
    await h.run_until(lambda: _in_status(h, "In-Progress"), label="a task is In-Progress")
    victim = (await h.by_status())["In-Progress"][0]
    await h.engine.tick()                                           # the work job starts
    await h.wait_for(lambda: bool(fake.calls_for("work")), label="work call in flight")
    await h.tasks.update(victim.id, status="Rejected", rejection_reason="User changed their mind")
    await h.settle()
    after = await h.tasks.get(victim.id)
    assert after.status == "Rejected" and after.deliverable == {}
    assert not [e for e in await h.event_list("submitted") if e.task_id == victim.id]
    assert all(a.state == "idle" for a in await h.agent_list())
    # the swarm carries on without it
    await h.run_to_state("VERIFICATION")
    final = {t.name: t.status for t in await h.task_list()}
    assert final[victim.name] == "Rejected" and "Done" in final.values()


async def test_user_deletes_a_task_while_the_judges_review_it(h, fake):
    fake.configure({"delay_ms": 150, "tasks_per_plan": 2})
    await start_empty_swarm(h)
    await h.run_until(lambda: _in_status(h, "Review"), label="a task is in Review")
    victim = (await h.by_status())["Review"][0]
    await h.engine.tick()                                           # the review job starts
    await h.wait_for(lambda: bool(fake.calls_for("review_quality")), label="review in flight")
    await h.tasks.delete(victim.id)
    await h.settle()
    assert await h.tasks.get(victim.id) is None
    assert not [e for e in await h.event_list("error")]
    await h.run_to_state("VERIFICATION")
    assert [t.status for t in await h.task_list()] == ["Done"]


async def test_user_rejects_the_candidate_while_mission_judgement_prioritises(h, fake):
    fake.configure({"delay_ms": 150, "tasks_per_plan": 3})
    await start_empty_swarm(h)
    await h.run_until(lambda: _in_status(h, "Pending"), label="tasks planned")
    first = (await h.task_list())[0]
    await h.engine.tick()                                           # the prioritize job starts
    await h.wait_for(lambda: bool(fake.calls_for("prioritize")), label="prioritize in flight")
    await h.tasks.update(first.id, status="Rejected", rejection_reason="Not wanted")
    await h.settle()
    by = await h.by_status()
    assert [t.id for t in by["Rejected"]] == [first.id] and first.id not in {
        t.id for t in by.get("Ready", [])}
    assert len(by["Ready"]) == 1                                    # a different task was approved
    await h.run_to_state("VERIFICATION")
    assert (await h.tasks.get(first.id)).status == "Rejected"


async def test_a_task_added_by_the_user_mid_run_is_picked_up(h, fake):
    fake.configure({"tasks_per_plan": 1})
    team = await seeded(h)
    await h.run_until(lambda: _in_status(h, "Ready"), label="first task approved")
    mine = await h.add_task(team, "User task", artifact="user.md")
    await h.run_to_state("VERIFICATION")
    got = await h.tasks.get(mine.id)
    assert got.status == "Done" and got.created_by == "user"
    assert h.workspace_file("user.md").startswith("# user.md")


# -- limits ---------------------------------------------------------------------------------

async def test_token_limit_pauses_the_swarm_and_resume_continues(h, fake, bus):
    fake.configure({"tasks_per_plan": 2})
    await start_empty_swarm(h, max_total_tokens=1200)
    await h.run_to_state("PAUSED")
    sw = await h.swarm()
    assert "Token limit reached" in sw.paused_reason and "max_total_tokens" in sw.paused_reason
    assert any("paused automatically" in e.detail for e in await h.event_list("swarm"))
    assert "swarm.paused" in [s.kind for s in await bus.recent_signals(20)]
    spent = len(fake.calls)

    for _ in range(3):                                              # paused: no spending
        assert await h.step() == []
    assert len(fake.calls) == spent

    await h.transition("resume")                                    # still over the limit
    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.PAUSE_FOR_LIMITS]
    assert (await h.state()) == "PAUSED" and len(fake.calls) == spent

    cfg = (await h.swarm()).config
    cfg.limits.max_total_tokens = 5_000_000                         # the user raises the limit
    await h.swarms.update(h.swarm_id, config=cfg)
    await h.transition("resume")
    assert (await h.swarm()).paused_reason is None
    await h.run_to_state("VERIFICATION")
    assert all(t.status == "Done" for t in await h.task_list())


async def test_wall_clock_and_budget_limits_pause_with_their_own_reasons(h, fake):
    cfg = swarm_config(max_wall_clock_minutes=30, max_total_tokens=None, max_budget_usd=0.001)
    cfg.model_prices = {"instant": 1.0, "fast": 1.0, "great": 1.0, "amazing": 1.0}
    await h.create_swarm(VISION, config=cfg, dod="- ok")
    await h.seed_team("Team A")
    await h.transition("start")
    await h.swarms.update(h.swarm_id, started_at=datetime.now(timezone.utc) - timedelta(hours=2))
    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.PAUSE_FOR_LIMITS]
    assert "Wall-clock limit reached" in (await h.swarm()).paused_reason
    assert fake.calls == []
    # budget: wall clock off, but the first call (priced) exceeds the tiny budget
    cfg2 = (await h.swarm()).config
    cfg2.limits.max_wall_clock_minutes = None
    await h.swarms.update(h.swarm_id, config=cfg2)
    await h.transition("resume")
    await h.run_to_state("PAUSED")
    assert "Budget limit reached" in (await h.swarm()).paused_reason


# -- gateway outage -------------------------------------------------------------------------------

async def test_outage_cools_the_job_down_and_recovers_without_counting_an_attempt(h, fake, clock):
    team = await seeded(h)
    agent = await delivery_agent(h)
    task = await h.add_task(team, "Held task", status="In-Progress", assigned_agent_id=agent.id,
                            committed_at=datetime.now(timezone.utc), complexity="simple")
    fake.configure({"outage_seconds": 600})

    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.WORK_TASK]
    assert [c["status"] for c in fake.calls] == [503, 503, 503]    # first try + 2 client retries
    errors = await h.event_list("error")
    assert len(errors) == 1
    assert "unreachable or overloaded" in errors[0].detail
    assert "retried automatically in 30 seconds" in errors[0].detail
    t = await h.tasks.get(task.id)
    assert t.attempts == 0 and t.status == "In-Progress" and t.feedback is None
    (call,) = await h.calls.list_for_agent(agent.id)
    assert call.ok is False and call.purpose == "work_task" and "503" in call.error
    assert (await h.agents.get(agent.id)).state == "idle"

    n = len(fake.calls)                                             # in cool-down: not retried
    for _ in range(2):
        assert await h.step() == []
    fake.configure({"outage_seconds": 0})                           # gateway is back...
    assert await h.step() == []                                     # ...but the cool-down holds
    assert len(fake.calls) == n

    clock.advance(31)
    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.WORK_TASK]
    assert (await h.tasks.get(task.id)).status == "Review"
    await h.run_to_state("VERIFICATION")
    done = await h.tasks.get(task.id)
    assert done.status == "Done" and done.attempts == 1             # the outage cost no attempt
    assert len(await h.event_list("error")) == 1                    # one event per streak


async def test_repeated_outage_failures_double_the_cooldown_but_log_once(h, fake, clock):
    await seeded(h)
    fake.configure({"outage_seconds": 600})
    await h.step()                                                  # plan fails (failure 1)
    key = "plan_tasks:" + (await h.team_list())[0].id + ":-"
    assert h.engine.cooldowns.failures(key) == 1
    clock.advance(31)
    await h.step()                                                  # failure 2: 60 s cool-down
    assert h.engine.cooldowns.failures(key) == 2
    clock.advance(31)
    assert await h.step() == []                                     # 31 s < 60 s: still cooling
    clock.advance(30)
    assert [a.kind for a in await h.step()] == [ActionKind.PLAN_TASKS]
    assert len(await h.event_list("error")) == 1                    # logged on the first failure only


async def test_outage_during_review_keeps_the_judge_reply_already_paid_for(h, fake, clock):
    """One judge answers, then the gateway dies: the retry asks only the other judge."""
    team = await seeded(h)
    agent = await delivery_agent(h)
    art = "held.md"
    ws = Path(h.settings.WORKSPACE_ROOT) / h.swarm_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / art).write_text("# Overview\n" + "word " * 40)
    task = await h.add_task(team, "Reviewed task", artifact=art, status="Review",
                            assigned_agent_id=agent.id,
                            deliverable={"summary": "done", "files": [art]})
    fake.configure({"fail_after_calls": 1})
    await h.step()
    assert (await h.tasks.get(task.id)).status == "Review"
    assert len(await h.event_list("error")) == 1
    fake.configure({"fail_after_calls": -1})
    clock.advance(31)
    await h.step()
    assert (await h.tasks.get(task.id)).status == "Done"
    ok = [c for c in fake.calls if c["job"] in ("review_quality", "review_mission")
          and c["status"] == 200]
    assert sorted(c["job"] for c in ok) == ["review_mission", "review_quality"]   # none paid twice


# -- parse failures ---------------------------------------------------------------------------------

async def test_three_consecutive_parse_failures_escalate_the_model(h, fake, clock):
    await h.create_swarm(VISION, teams=[("Docs", "To advance the Vision, write docs.")],
                         dod="- ok", config=swarm_config(), start=True)
    fake.configure({"malformed_next": 6})            # 3 jobs x (first reply + one re-ask)
    for failure in (1, 2, 3):
        actions = await h.step()
        assert [a.kind for a in actions] == [ActionKind.GENERATE_STANDARDS], failure
        assert (await h.team_list())[0].definition_of_done is None
        clock.advance(400)
    errors = await h.event_list("error")
    assert "unusable answer" in errors[0].detail
    assert any("more capable model" in e.detail for e in errors)
    assert [c["model"] for c in fake.calls] == ["great"] * 6
    # the fourth attempt asks one rung higher (and the fake now answers properly)
    await h.step()
    assert fake.calls[-1]["model"] == "amazing" and fake.calls[-1]["role"] == "Quality Judgement"
    assert (await h.team_list())[0].definition_of_done
    assert not h.engine.escalation                                  # forgotten after success
    calls = await h.calls.list_for_swarm(h.swarm_id)
    assert sum(1 for c in calls if not c.ok) == 3


async def test_one_malformed_reply_is_repaired_by_the_reask(h, fake):
    await h.create_swarm(VISION, teams=[("Docs", "To advance the Vision, write docs.")],
                         dod="- ok", config=swarm_config(), start=True)
    fake.configure({"malformed_next": 1})
    await h.step()
    assert (await h.team_list())[0].definition_of_done                # first step succeeded
    assert [c["reask"] for c in fake.calls] == [False, True]
    assert not await h.event_list("error")


# -- B.5: blocking verdicts must name a gap (the re-ask path) -------------------------------------

async def test_mission_judge_blocking_without_a_gap_is_asked_again_and_the_task_finishes(h, fake):
    fake.configure({"empty_feedback_first": 1, "tasks_per_plan": 1})
    await start_empty_swarm(h)
    await h.run_to_state("VERIFICATION")
    (task,) = await h.task_list()
    assert task.status == "Done" and task.attempts == 1             # not reworked blind
    mission = fake.calls_for("review_mission")
    assert [c["reask"] for c in mission] == [False, True]
    assert not await h.event_list("error") and not await h.event_list("rework")
    # both replies are in the call transcript (the sloppy one and the corrected one)
    (call,) = [c for c in await h.calls.list_for_swarm(h.swarm_id)
               if c.purpose == "review_mission"]
    assert call.total_tokens == 300


async def test_assessment_without_a_concrete_next_focus_is_asked_again(h, fake):
    team = await seeded(h)
    await h.add_task(team, "Old task", status="Done", done_at=datetime.now(timezone.utc))
    fake.configure({"empty_feedback_first": 1, "plan_rounds": 5})
    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.ASSESS_MISSION]
    assert [c["reask"] for c in fake.calls_for("assess")] == [False, True]
    (team,) = await h.team_list()
    assert team.next_focus == "Cover the next part of the Mission." and team.last_assessed_at
    assert not await h.event_list("error")


# -- dead dependencies ------------------------------------------------------------------------------

async def test_a_task_behind_a_failed_dependency_does_not_stall_the_team(h, fake):
    fake.configure({"tasks_per_plan": 1})
    team = await seeded(h)
    dead = await h.add_task(team, "Dead task", status="Failed", attempts=3)
    behind = await h.add_task(team, "Behind the dead one", depends_on=[dead.id])
    missing = await h.add_task(team, "Behind a missing one", depends_on=["no-such-task"])
    await h.run_to_state("VERIFICATION")
    for t in (behind, missing):
        status = (await h.tasks.get(t.id)).status
        assert status in ("Pending", "Rejected"), status             # never started
        assert status != "Ready"
    assert [c["task_id"] for c in fake.calls_for("work")
            if c["task_id"] in (behind.id, missing.id)] == []
    assert (await h.team_list())[0].status == "accomplished"        # the new work was done
    assert any(t.name == "Team A task 1" and t.status == "Done" for t in await h.task_list())
