"""The engine in-process, phase by phase, against the scripted fake LLM (docs/DESIGN-0.2.md A.2).

A swarm is stepped from CREATED to VERIFICATION: bootstrap, standards, plan, prioritize,
commit, work, review, rework, Done, assess, complete. After each phase the database is checked.
"""
from __future__ import annotations

from app.models import ActionKind
from app.repo import MemoryRepo
from tests.integration.helpers import swarm_config

VISION = "Write a short guide to compost."


async def start_empty_swarm(h, **cfg):
    """A started swarm with no teams and no DoD: the coordinator bootstraps everything."""
    await h.create_swarm(VISION, config=swarm_config(**cfg))
    await h.transition("start")


async def test_full_run_phase_by_phase(h, fake):
    fake.configure({"tasks_per_plan": 2})
    await start_empty_swarm(h)

    # bootstrap: coordinator writes the vision DoD and defines the team
    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.BOOTSTRAP]
    swarm = await h.swarm()
    assert swarm.definition_of_done and "deliverable" in swarm.definition_of_done
    (team,) = await h.team_list()
    assert team.name == "Team A" and "Vision" in team.mission_statement
    assert team.creator_kind == "swarm_coordinator" and team.definition_of_done is None
    agents = await h.agent_list()
    assert sorted(a.role for a in agents) == [
        "mission_delivery", "mission_judgement", "quality_judgement", "swarm_coordinator"]
    assert all(a.state == "idle" for a in agents)
    assert [c["job"] for c in fake.calls] == ["bootstrap"]
    assert fake.calls[0]["model"] == "amazing"                      # coordinator model

    # standards: Quality Judgement sets the team DoR and DoD
    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.GENERATE_STANDARDS]
    (team,) = await h.team_list()
    assert team.definition_of_ready and team.definition_of_done
    assert fake.calls[-1]["role"] == "Quality Judgement" and fake.calls[-1]["model"] == "great"

    # plan: the delivery agent plans two Pending tasks with criteria
    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.PLAN_TASKS]
    tasks = await h.task_list()
    assert [t.name for t in tasks] == ["Team A task 1", "Team A task 2"]
    assert all(t.status == "Pending" and t.created_by != "user" for t in tasks)
    kinds = [c.check_kind for c in tasks[0].criteria]
    assert kinds == ["file_exists", "min_words", "file_contains", "rubric"]
    assert fake.calls[-1]["role"] == "Mission Delivery"

    # prioritize: Mission Judgement approves the next task (one delivery agent -> one slot)
    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.PRIORITIZE]
    by = await h.by_status()
    assert len(by["Ready"]) == 1 and len(by["Pending"]) == 1
    ready = by["Ready"][0]
    assert ready.priority == 1 and ready.approved_at is not None
    assert by["Pending"][0].priority == 2

    # commit (inline): DoR met -> In-Progress with an owner
    actions = await h.step()
    assert ActionKind.COMMIT_TASK in [a.kind for a in actions]
    task = (await h.by_status())["In-Progress"][0]
    delivery = next(a for a in await h.agent_list() if a.role == "mission_delivery")
    assert task.id == ready.id and task.assigned_agent_id == delivery.id
    assert task.committed_at is not None

    # work: classify, then the tool loop writes the files and submits
    await h.step()
    task = (await h.by_status())["Review"][0]
    assert task.complexity == "simple" and task.deliverable["files"] == ["team-a-part-1.md"]
    assert task.deliverable["model"] == "fast" and "criterion" in task.deliverable["summary"]
    ws = h.settings.WORKSPACE_ROOT + f"/{task.swarm_id}/team-a-part-1.md"
    text = open(ws).read()
    assert "Overview" in text and len(text.split()) >= 28
    work_calls = fake.calls_for("work")
    assert [c["has_tools"] for c in work_calls] == [True, True]       # write_file, submit
    assert [c["job"] for c in fake.calls if c["job"] == "classify"] == ["classify"]
    assert (await h.agents.get(delivery.id)).state == "idle"

    # review: deterministic checks, then both judges; the task is Done
    await h.step()
    task = await h.tasks.get(task.id)
    assert task.status == "Done" and task.attempts == 1 and task.done_at is not None
    assert [c.status for c in task.criteria] == ["green"] * 4
    assert task.criteria[0].evidence.startswith("File 'team-a-part-1.md' exists")
    assert task.criteria[3].score == 1.0
    verdicts = await h.verdicts.list_for_task(task.id)
    assert sorted(v.judge_role for v in verdicts) == ["mission_judgement", "quality_judgement"]
    assert all(v.passed for v in verdicts)
    assert {"review_quality", "review_mission"} <= {c["job"] for c in fake.calls}

    # events of the task, in order
    ev = [e.kind for e in await h.event_list() if e.task_id == task.id]
    assert ev == ["approved", "committed", "submitted", "verdict", "verdict", "done"]

    # second task flows the same way, then the team has no open tasks
    await h.run_until(lambda: _all_done(h), label="both tasks Done")
    (team,) = await h.team_list()
    assert team.status == "active" and team.last_assessed_at is None

    # assess: Mission Judgement says accomplished; the team is accomplished
    await h.run_until(lambda: _team_status(h, "accomplished"), label="team accomplished")
    (team,) = await h.team_list()
    assert team.last_assessed_at is not None
    assert (await h.event_list("assessment"))[0].detail.count("accomplished") >= 1

    # complete: all teams terminal -> VERIFICATION
    await h.run_to_state("VERIFICATION")
    sw = await h.swarm()
    assert sw.started_at is not None
    assert (await h.event_list("swarm"))[-1].detail.startswith("All teams have finished")
    assert await h.step() == []                                     # nothing left to do

    # accounting: every call row has tokens; agents counted their calls; no errors
    calls = await h.calls.list_for_swarm(sw.id)
    assert calls and all(c.ok and c.total_tokens > 0 for c in calls)
    assert {c.purpose for c in calls} >= {
        "bootstrap", "generate_standards", "plan_tasks", "prioritize", "classify_task",
        "work_task", "review_quality", "review_mission", "assess_mission"}
    assert sum(c.total_tokens for c in calls) == await h.calls.tokens_used(sw.id)
    assert all(a.state == "idle" for a in await h.agent_list())
    assert not [e for e in await h.event_list("error")]
    # the fake saw exactly one bootstrap / standards / plan / assess for the single team
    for job, n in (("bootstrap", 1), ("standards", 1), ("plan", 1), ("assess", 1)):
        assert len(fake.calls_for(job)) == n, job
    assert len(fake.calls_for("review_quality")) == 2


async def _all_done(h):
    by = await h.by_status()
    return len(by.get("Done", [])) == 2


async def _team_status(h, status):
    return (await h.team_list())[0].status == status


async def test_user_supplied_teams_and_dod_skip_bootstrap_of_teams(h, fake):
    """Teams and a vision DoD given at creation: the coordinator is not asked for teams
    (or, with a DoD too, not asked at all)."""
    await h.create_swarm(VISION, teams=[("Docs", "To advance the Vision, write docs.")],
                         dod="- Guide exists", start=True)
    await h.step()
    assert [c["job"] for c in fake.calls] == ["standards"]           # straight to standards
    await h.run_to_state("VERIFICATION")
    assert not fake.calls_for("bootstrap")
    assert (await h.team_list())[0].creator_kind == "user"


async def test_bootstrap_for_given_teams_asks_only_for_the_dod(h, fake):
    await h.create_swarm(VISION, teams=[("Docs", "To advance the Vision, write docs.")], start=True)
    await h.step()
    assert fake.calls[0]["job"] == "bootstrap"
    assert [t.name for t in await h.team_list()] == ["Docs"]         # no extra teams invented
    assert (await h.swarm()).definition_of_done


async def test_multiple_teams_and_plan_rounds(h, fake):
    """Two teams work side by side; each plans twice before Mission Judgement is satisfied."""
    fake.configure({"teams": 2, "plan_rounds": 2, "tasks_per_plan": 1})
    await start_empty_swarm(h)
    await h.run_to_state("VERIFICATION")
    teams = await h.team_list()
    assert [t.name for t in teams] == ["Team A", "Team B"]
    assert all(t.status == "accomplished" for t in teams)
    by = await h.by_status()
    assert len(by["Done"]) == 4 and set(by) == {"Done"}
    # each team's first assessment said "continue" (with a focus), the second "accomplished"
    assessments = [e for e in await h.event_list("assessment")]
    assert sum("more work needed" in e.detail for e in assessments) == 2
    assert sum("accomplished its mission" in e.detail for e in assessments) == 2
    # the next plan carries the coordinator's focus (set by the continue assessment)
    assert fake.calls_for("plan")[2]["team"] in ("Team A", "Team B")
    names = sorted(t.name for t in await h.task_list())
    assert names == ["Team A task 1", "Team A task 2", "Team B task 1", "Team B task 2"]


async def test_sibling_teams_are_named_in_planning_prompts(h, fake):
    """Teams see each other's Missions (scope block) when they plan."""
    fake.configure({"teams": 2, "record_prompts": True})
    await start_empty_swarm(h)
    await h.run_until(lambda: _planned(h), label="both teams planned")
    plans = fake.calls_for("plan")
    assert len(plans) == 2
    for call in plans:
        other = "Team B" if call["team"] == "Team A" else "Team A"
        assert "Other teams in this swarm own these Missions" in call["user"]
        assert f'"{other}"' in call["user"] and f'"{call["team"]}" (' not in call["user"]


async def _has_tasks(h):
    return bool(await h.task_list())


async def _planned(h):
    return len({t.team_id for t in await h.task_list()}) == 2


async def test_dependencies_are_respected(h, fake):
    fake.configure({"plan_dependencies": "chain", "tasks_per_plan": 3})
    await start_empty_swarm(h)
    order = []
    seen = set()

    async def watch():
        for t in await h.task_list():
            if t.status == "Done" and t.id not in seen:
                seen.add(t.id)
                order.append(t.name)
        return (await h.state()) == "VERIFICATION"

    await h.run_until(watch, label="verification")
    assert order == ["Team A task 1", "Team A task 2", "Team A task 3"]
    tasks = {t.name: t for t in await h.task_list()}
    assert tasks["Team A task 2"].depends_on == [tasks["Team A task 1"].id]
    assert tasks["Team A task 2"].committed_at >= tasks["Team A task 1"].done_at


async def test_rework_then_done(h, fake):
    """Quality Judgement blocks the first attempt: rework with feedback, then Done."""
    fake.configure({"review_fail_attempts": 1, "tasks_per_plan": 1})
    await start_empty_swarm(h)
    await h.run_to_state("VERIFICATION")
    (task,) = [t for t in await h.task_list() if t.status == "Done"]
    assert task.attempts == 2 and task.feedback is None
    rework = [e for e in await h.event_list("rework")]
    assert len(rework) == 1 and "concluding paragraph" in rework[0].detail
    works = fake.calls_for("work")
    assert [c["feedback_in_prompt"] for c in works if c["task_id"] == task.id][-1] is True
    assert works[0]["feedback_in_prompt"] is False
    assert len(fake.calls_for("review_quality")) == 2
    verdicts = await h.verdicts.list_for_task(task.id)
    assert [(v.attempt, v.passed) for v in verdicts if v.judge_role == "quality_judgement"] == [
        (1, False), (2, True)]
    # a task that needed more than one attempt leaves a pattern learning
    learnings = [x for x in await _learnings(h) if x.task_id == task.id]
    assert learnings and learnings[0].kind == "pattern" and "2 attempts" in learnings[0].content


async def _learnings(h):
    from app.repo import LearningRepo
    return await LearningRepo(h.pool).list(h.swarm_id)


async def test_mission_judgement_can_block_too(h, fake):
    fake.configure({"review_fail_attempts": 1, "review_fail_judge": "mission",
                    "tasks_per_plan": 1})
    await start_empty_swarm(h)
    await h.run_to_state("VERIFICATION")
    (task,) = await h.task_list()
    assert task.status == "Done" and task.attempts == 2
    assert "stakeholder" in (await h.event_list("rework"))[0].detail


async def test_task_fails_after_max_attempts_and_team_fails_at_its_budget(h, fake):
    """Judges always block: each task Failed after max_attempts; the team spends its task
    budget and fails; the swarm still reaches VERIFICATION."""
    fake.configure({"review_fail_attempts": 99, "tasks_per_plan": 2, "plan_rounds": 5})
    await start_empty_swarm(h, max_attempts_per_task=2, max_tasks_per_team=2)
    await h.run_to_state("VERIFICATION")
    tasks = await h.task_list()
    assert {t.status for t in tasks} == {"Failed"} and all(t.attempts == 2 for t in tasks)
    (team,) = await h.team_list()
    assert team.status == "failed"
    kinds = [e.kind for e in await h.event_list()]
    assert kinds.count("failed") == 2
    assert any("failed: it used its whole budget" in e.detail for e in await h.event_list("team"))
    assert [x for x in await _learnings(h) if "Plan fewer" in x.content]
    assert (await h.swarm()).lifecycle_state == "VERIFICATION"
    # the team was assessed (to make sure nothing was missed) before being failed
    assert len(fake.calls_for("assess")) == 1


async def test_work_without_submit_counts_as_a_failed_attempt(h, fake):
    fake.configure({"work_submit": False, "tasks_per_plan": 1})
    await start_empty_swarm(h, max_attempts_per_task=2, max_tasks_per_team=1)
    await h.run_to_state("VERIFICATION")
    (task,) = await h.task_list()
    assert task.status == "Failed" and task.attempts == 2
    assert "without calling `submit`" in task.feedback and task.deliverable["files"]
    assert len(fake.calls_for("review_quality")) == 0               # never reached review


async def test_task_model_escalates_with_failed_attempts(h, fake):
    fake.configure({"review_fail_attempts": 2, "tasks_per_plan": 1})
    await start_empty_swarm(h)
    await h.run_to_state("VERIFICATION")
    models = [c["model"] for c in fake.calls_for("work")
              if c["has_tools"] and c["task_id"]][::2]                # first call of each attempt
    assert models == ["fast", "great", "amazing"]
    assert (await h.task_list())[0].deliverable["model"] == "amazing"


async def test_complexity_classification_picks_the_model(h, fake):
    fake.configure({"complexity": "complex", "tasks_per_plan": 1})
    await start_empty_swarm(h)
    await h.run_to_state("VERIFICATION")
    assert (await h.task_list())[0].complexity == "complex"
    assert {c["model"] for c in fake.calls_for("work")} == {"great"}  # one rung above "fast"
    assert fake.calls_for("classify")[0]["model"] == "instant"


async def test_mission_assessment_restart_bumps_generation_and_rejects_open_work(h, fake):
    fake.configure({"assess_restart_count": 1, "tasks_per_plan": 1})
    await start_empty_swarm(h)
    await h.run_to_state("VERIFICATION")
    (team,) = await h.team_list()
    assert team.generation == 1 and team.status == "accomplished"
    assert [x for x in await _learnings(h) if "restarted its approach" in x.content]
    assert len(fake.calls_for("plan")) == 2                         # planned again after restart
    assert any("restarted team" in e.detail for e in await h.event_list("assessment"))


async def test_low_confidence_accomplished_is_treated_as_continue(h, fake):
    fake.configure({"accomplished_confidence": 0.5, "tasks_per_plan": 1})
    await start_empty_swarm(h, max_tasks_per_team=3)
    await h.run_to_state("VERIFICATION")
    (team,) = await h.team_list()
    assert team.status == "failed"                                  # never convinced; budget spent
    assert any("only with confidence 0.50" in e.detail for e in await h.event_list("assessment"))
    assert len(await h.task_list()) == 3


async def test_remember_tool_persists_agent_memory(h, fake):
    fake.configure({"work_remember": True, "tasks_per_plan": 1})
    await start_empty_swarm(h)
    await h.run_to_state("VERIFICATION")
    delivery = next(a for a in await h.agent_list() if a.role == "mission_delivery")
    mems = await MemoryRepo(h.pool).list(delivery.id)
    assert [m.content for m in mems] == ["The fake agent remembers this task."]


async def test_global_values_and_feedback_reach_the_system_prompt(h, fake):
    from app.models import GlobalConfig
    from app.repo import ConfigRepo, FeedbackRepo

    await ConfigRepo(h.pool).put_global(GlobalConfig(values_principles="Always be kind."))
    fake.configure({"record_prompts": True})
    await start_empty_swarm(h)
    await FeedbackRepo(h.pool).add(h.swarm_id, "Prefer short sentences.")
    await h.run_until(lambda: _has_tasks(h), label="first plan")
    system = fake.calls_for("plan")[0]["system"]
    order = [system.index(f"# {s}") for s in (
        "Vision", "Values and Principles", "Mission", "Definition of Done", "Your Role",
        "User Feedback")]
    assert order == sorted(order)
    assert "Always be kind." in system and "Prefer short sentences." in system
    assert VISION in system.split("# Values")[0]


async def test_retrospective_after_learn_then_archive(h, fake):
    fake.configure({"tasks_per_plan": 1})
    await start_empty_swarm(h)
    await h.run_to_state("VERIFICATION")
    await h.transition("learn")
    assert (await h.swarm()).retrospective_done is False
    actions = await h.step()
    assert [a.kind for a in actions] == [ActionKind.RETROSPECTIVE]
    sw = await h.swarm()
    assert sw.retrospective_done is True and sw.lifecycle_state == "LEARNING"
    glob = [x for x in await _learnings(h) if x.swarm_id is None]
    assert {x.kind for x in glob} == {"pattern", "routing"}
    assert fake.calls_for("retrospective")[0]["role"] == "Swarm Coordinator"
    assert await h.step() == []                                     # not repeated
    await h.transition("archive")
    assert (await h.swarm()).lifecycle_state == "ARCHIVED"
    assert await h.engine.tick() == []                              # archived: engine is idle
