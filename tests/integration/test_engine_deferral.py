"""Deferral: Mission Judgement may approve nothing when no candidate can start yet.

Found in a live run (First Garden, v0.2): a team whose work needs other teams' artifacts had its
tasks rejected one by one ("Definition of Ready not met"), re-planned, and used up its task budget.
Now Mission Judgement answers `"next_task_ids": []`, the candidates stay Pending, and the engine
holds that team's prioritize job until another task in the swarm is Done or DEFER_SECONDS pass.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.engine import DEFER_SECONDS
from app.models import ActionKind
from tests.integration.helpers import swarm_config


async def seeded(h, delivery=1):
    await h.create_swarm("Write a short guide to compost.", config=swarm_config(delivery),
                         dod="- The guide exists")
    team = await h.seed_team("Team A")
    await h.transition("start")
    return team


async def test_a_deferred_task_stays_pending_and_is_held_without_a_hot_loop(h, fake):
    team = await seeded(h)
    other = await h.seed_team("Team B")
    task = await h.add_task(team, "Checklist")
    agent_b = next(a for a in await h.agent_list(other) if a.role == "mission_delivery")
    calendar = await h.add_task(other, "Calendar", status="In-Progress",
                                assigned_agent_id=agent_b.id,
                                committed_at=datetime.now(timezone.utc), complexity="simple")
    fake.configure({"defer_prioritize": 1, "delay_ms": 400})       # Team B works slowly

    actions = await h.engine.tick()                                 # A prioritizes, B works
    assert (ActionKind.PRIORITIZE, team.id) in [(a.kind, a.team_id) for a in actions]
    await h.wait_for(lambda: bool(h.engine.deferrals.active_keys()), label="deferral held")
    assert (await h.tasks.get(task.id)).status == "Pending"         # not rejected, not approved
    deferred = await h.event_list("deferred")
    assert len(deferred) == 1 and "'Checklist'" in deferred[0].detail
    assert not await h.event_list("rejected")

    for _ in range(3):                                              # held: no new calls
        actions = await h.engine.tick()
        assert ActionKind.PRIORITIZE not in [a.kind for a in actions]
    assert len(fake.calls_for("prioritize")) == 1
    assert (await h.tasks.get(calendar.id)).status != "Done"

    h.clock.advance(DEFER_SECONDS + 1)                              # asked again after the wait
    actions = await h.engine.tick()
    assert (ActionKind.PRIORITIZE, team.id) in [(a.kind, a.team_id) for a in actions]
    await h.settle()
    fake.configure({"delay_ms": 0})
    await h.run_to_state("VERIFICATION")
    assert (await h.tasks.get(task.id)).status == "Done"


async def test_with_nothing_else_in_flight_mission_judgement_cannot_defer(h, fake):
    """Waiting could never change the answer (no other work can finish), so a deferral would
    loop forever: the prompt offers no deferral and an empty answer approves the first task."""
    fake.configure({"defer_prioritize": 1, "record_prompts": True})
    team = await seeded(h)
    task = await h.add_task(team, "Polish the checklist")
    await h.step()
    assert (await h.tasks.get(task.id)).status == "Ready"
    assert not await h.event_list("deferred")
    assert "waiting cannot change anything" in fake.calls_for("prioritize")[0]["user"]


async def test_a_deferral_is_released_as_soon_as_other_work_is_done(h, fake):
    team = await seeded(h)
    first = await h.add_task(team, "Calendar")
    second = await h.add_task(team, "Checklist")
    await h.step()                                                  # one slot: Calendar approved
    assert (await h.tasks.get(first.id)).status == "Ready"
    fake.configure({"defer_prioritize": 1})                         # Checklist must wait

    async def calendar_done():
        return (await h.tasks.get(first.id)).status == "Done"

    await h.run_until(calendar_done, label="Calendar Done")
    assert (await h.tasks.get(second.id)).status == "Pending"
    assert len(await h.event_list("deferred")) == 1
    assert len(fake.calls_for("prioritize")) == 2                   # held while Calendar ran

    async def checklist_approved():                                 # no clock advance needed
        return (await h.tasks.get(second.id)).status != "Pending"

    await h.run_until(checklist_approved, label="Checklist approved")
    assert len(fake.calls_for("prioritize")) == 3
    await h.run_to_state("VERIFICATION")
    assert {t.status for t in await h.task_list()} == {"Done"}
    assert not await h.event_list("rejected")


async def test_the_prompt_lists_the_workspace_files_of_every_team(h, fake):
    from pathlib import Path
    fake.configure({"record_prompts": True})
    team = await seeded(h)
    ws = Path(h.settings.WORKSPACE_ROOT) / h.swarm_id
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "calendar.md").write_text("# Calendar\n")
    await h.add_task(team, "Checklist")
    await h.step()
    assert "calendar.md (11 bytes)" in fake.calls_for("prioritize")[0]["user"]
