"""Vision review (docs/DESIGN-0.3.md section 10): all teams terminal -> the Swarm Coordinator
judges the combined result before the swarm goes to VERIFICATION."""
from __future__ import annotations

from app.models import ActionKind
from tests.integration.helpers import swarm_config

VISION = "Write a short guide to compost."


async def seeded(h, teams=("Team A",), **limits):
    await h.create_swarm(VISION, config=swarm_config(1, **limits), dod="- The guide exists")
    out = [await h.seed_team(n) for n in teams]
    await h.transition("start")
    return out


async def test_happy_path_goes_through_one_vision_review(h, fake):
    fake.configure({"tasks_per_plan": 1, "record_prompts": True})
    await seeded(h)
    await h.run_to_state("VERIFICATION")
    (call,) = fake.calls_for("review_vision")
    assert call["role"] == "Swarm Coordinator" and call["model"] == "amazing"
    assert "- The guide exists" in call["user"] and 'Team "Team A"' in call["user"]
    assert (await h.swarm()).vision_reviews == 1
    kinds = [k for step in h.history for k in step]
    assert kinds.count("review_vision") == 1 and "complete_swarm" not in kinds
    details = [e.detail for e in await h.event_list("swarm")]
    assert any("confirmed the vision-level Definition of Done" in d for d in details)
    assert any("moved to verification" in d for d in details)


async def test_gaps_reopen_the_team_which_plans_with_the_focus_then_passes(h, fake):
    fake.configure({"tasks_per_plan": 1, "vision_gaps_first": 1, "record_prompts": True})
    (team,) = await seeded(h)
    await h.run_to_state("VERIFICATION")
    assert len(fake.calls_for("review_vision")) == 2
    assert (await h.swarm()).vision_reviews == 2
    reopened = [e for e in await h.event_list("team") if "reopened team 'Team A'" in e.detail]
    assert len(reopened) == 1 and "integration section" in reopened[0].detail
    plans = fake.calls_for("plan")
    assert len(plans) == 2
    assert "integration section" in plans[1]["user"]               # the focus reached planning
    done = [t for t in await h.task_list(team) if t.status == "Done"]
    assert len(done) == 2
    assert (await h.team_list())[0].status == "accomplished"
    # The reopen was followed by PLAN_TASKS, not by another review or completion.
    flat = [k for step in h.history for k in step]
    i = flat.index("review_vision")
    assert "plan_tasks" in flat[i + 1:] and flat.count("review_vision") == 2


async def test_the_cap_completes_after_three_reviews_even_with_gaps(h, fake):
    fake.configure({"tasks_per_plan": 1, "vision_gaps_first": 10})
    await seeded(h, max_tasks_per_team=10)
    await h.run_to_state("VERIFICATION")
    assert len(fake.calls_for("review_vision")) == 3
    assert (await h.swarm()).vision_reviews == 3
    flat = [k for step in h.history for k in step]
    assert flat.count("review_vision") == 3 and "complete_swarm" in flat


async def test_a_failed_team_without_budget_is_not_reopened(h, fake):
    fake.configure({"tasks_per_plan": 1, "vision_gaps_first": 1})
    team_a, _ = await seeded(h, teams=("Team A", "Team B"), max_tasks_per_team=1)
    await h.add_task(team_a, "Only task", status="Failed", attempts=3)
    await h.teams.update(team_a.id, status="failed")
    await h.run_to_state("VERIFICATION")
    assert len(fake.calls_for("review_vision")) == 1               # nothing reopened: no 2nd review
    assert (await h.swarm()).vision_reviews == 1
    teams = {t.name: t for t in await h.team_list()}
    assert teams["Team A"].status == "failed" and teams["Team B"].status == "accomplished"
    details = " ".join(e.detail for e in await h.event_list("swarm"))
    assert "no team could act on" in details and "Team A" in details


async def test_review_vision_discarded_when_the_swarm_is_no_longer_active(h, fake):
    fake.configure({"tasks_per_plan": 1, "delay_ms": 0})
    await seeded(h)
    await h.run_until(lambda: _teams_done(h), label="team accomplished")
    fake.configure({"delay_ms": 300})
    await h.engine.tick()                                           # REVIEW_VISION starts
    await h.wait_for(lambda: bool(fake.calls_for("review_vision")), label="review call")
    await h.transition("pause")
    await h.settle()
    sw = await h.swarm()
    assert sw.lifecycle_state == "PAUSED" and sw.vision_reviews == 0


async def _teams_done(h) -> bool:
    return all(t.status == "accomplished" for t in await h.team_list())


def test_action_kind_exists():
    assert ActionKind.REVIEW_VISION.value == "review_vision"
