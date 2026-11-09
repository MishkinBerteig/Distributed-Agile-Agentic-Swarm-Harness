"""Agent capabilities against real PostgreSQL (docs/DESIGN-0.3.md section 11)."""
from __future__ import annotations

import pytest

from app.models import HarnessSpec
from tests.integration.helpers import swarm_config
from tests.integration.runner_helpers import FakeRunner, make_remote_job, new_runner
from tests.integration.test_engine_remote import (VISION, jobs_of, runner, stop_engine,
                                                  tick_until)


@pytest.fixture(autouse=True)
def fast_poll(h):
    h.engine.jobs.remote_poll_seconds = 0.02


def cfg_for(harnesses, **limits):
    cfg = swarm_config(len(harnesses), **limits)
    cfg.allowed_harnesses = ["daash", "fake"]
    cfg.delivery_harnesses = [HarnessSpec(harness=x, model="") for x in harnesses]
    return cfg


async def ready_team(h, harnesses, tasks):
    """A started swarm with a team and Ready tasks: [(name, required_capabilities)]."""
    await h.create_swarm(VISION, config=cfg_for(harnesses), dod="- The guide exists")
    team = await h.seed_team("Team A")
    made = [await h.add_task(team, n, status="Ready", required_capabilities=req,
                             complexity="moderate") for n, req in tasks]
    await h.transition("start")
    return team, made


async def test_agents_get_harness_default_capabilities(h):
    await h.create_swarm(VISION, config=cfg_for(["daash", "fake"]), dod="- d")
    team = await h.seed_team("Team A")
    caps = {a.name: a.capabilities for a in await h.agent_list(team)}
    assert caps["Delivery-1"] == ["edit_files"]
    assert caps["Delivery-2"] == ["edit_files", "run_code"]
    assert caps["Mission Judgement"] == ["edit_files"]


async def test_spec_override_and_api_json(h, api):
    cfg = swarm_config(1)
    cfg.allowed_harnesses = ["daash", "fake"]
    cfg.delivery_harnesses = [HarnessSpec(harness="fake", model="", capabilities=["edit_files"])]
    await h.create_swarm(VISION, config=cfg, dod="- d")
    team = await h.seed_team("Team A")
    (d,) = [a for a in await h.agent_list(team) if a.role == "mission_delivery"]
    assert d.capabilities == ["edit_files"]
    got = (await api.get(f"/teams/{team.id}")).json()["agents"]
    assert all("capabilities" in a for a in got)
    assert [a["capabilities"] for a in got if a["name"] == "Delivery-1"] == [["edit_files"]]


async def test_post_task_accepts_required_capabilities(h, api):
    await h.create_swarm(VISION, config=cfg_for(["daash"]), dod="- d")
    team = await h.seed_team("Team A")
    r = await api.post("/tasks", json={
        "team_id": team.id, "name": "Verify", "required_capabilities": ["run_code", "bogus"],
        "acceptance_criteria": [{"text": "ok"}]})
    assert r.status_code == 201, r.text
    assert r.json()["required_capabilities"] == ["run_code"]


async def test_run_code_task_goes_to_remote_agent_never_native(h, api, pool):
    team, (task,) = await ready_team(h, ["daash", "fake"], [("Verify", ["run_code"])])
    fr = (await runner(api)).start()
    try:
        await h.run_until(
            lambda: _status(h, task, "Done"), label="task done", max_steps=300)
    finally:
        await fr.close()
        await stop_engine(h)
    done = await h.tasks.get(task.id)
    agent = await h.agents.get(done.assigned_agent_id)
    assert agent.name == "Delivery-2" and agent.harness == "fake"
    assert len(await jobs_of(pool, task.id)) == 1
    assert not [c for c in h.calls_of("work") if c["task_id"] == task.id]


async def _status(h, task, status):
    return (await h.tasks.get(task.id)).status == status


async def test_native_takes_other_work_while_capable_agent_is_busy(h, api, pool):
    team, (t1, t2) = await ready_team(
        h, ["daash", "fake"], [("Verify one", ["run_code"]), ("Write two", [])])
    await tick_until(h, lambda: _both_committed(h, t1, t2), "both committed")
    a1 = await h.agents.get((await h.tasks.get(t1.id)).assigned_agent_id)
    a2 = await h.agents.get((await h.tasks.get(t2.id)).assigned_agent_id)
    assert a1.harness == "fake"
    await stop_engine(h)
    assert a2.harness == "daash"


async def _both_committed(h, t1, t2):
    for t in (t1, t2):
        if (await h.tasks.get(t.id)).status == "Ready":
            return False
    return True


async def test_degraded_fallback_when_no_agent_can(h, fake):
    fake.configure({"record_prompts": True})
    team, (task,) = await ready_team(h, ["daash"], [("Verify", ["run_code"])])
    await h.run_until(lambda: _status(h, task, "Done"), label="degraded task done")
    evs = [e for e in await h.event_list("info") if e.task_id == task.id]
    assert len(evs) == 1 and "degraded" in evs[0].detail and "run_code" in evs[0].detail
    prompt = [c for c in h.calls_of("work") if c["task_id"] == task.id][0]["user"]
    assert "Your capabilities: edit_files" in prompt
    assert "needs run_code, which you do not have" in prompt


async def test_review_prompts_state_author_capabilities(h, fake):
    fake.configure({"record_prompts": True})
    team, (task,) = await ready_team(h, ["daash"], [("Write", [])])
    await h.run_until(lambda: _status(h, task, "Done"), label="task done")
    q = h.calls_of("review_quality")[0]["user"]
    assert "delivering agent's capabilities: edit_files" in q
    assert "delivering agent's capabilities: edit_files" in h.calls_of("review_mission")[0]["user"]


async def test_plan_needs_reach_the_task_and_planner_sees_capabilities(h, fake, api):
    fake.configure({"record_prompts": True, "plan_needs": "run_code", "tasks_per_plan": 2})
    await h.create_swarm(VISION, config=cfg_for(["daash", "fake"]), dod="- d")
    await h.seed_team("Team A")
    await h.transition("start")
    await h.run_until(lambda: _has_tasks(h), label="planned")
    tasks = await h.task_list()
    assert [t.required_capabilities for t in tasks[:2]] == [["run_code"], []]
    plan_prompt = h.calls_of("plan")[0]["user"]
    assert "Delivery agent 2: edit_files, run_code" in plan_prompt
    rows = (await api.get("/tasks", params={"swarm_id": h.swarm_id})).json()["tasks"]
    assert rows and "required_capabilities" in rows[0]
    await stop_engine(h)


async def _has_tasks(h):
    return bool(await h.task_list())


async def test_rework_reassigns_to_a_capable_idle_agent(h):
    team, (task,) = await ready_team(h, ["daash", "fake"], [("Verify", ["run_code"])])
    agents = {a.name: a for a in await h.agent_list(team) if a.role == "mission_delivery"}
    native, remote_agent = agents["Delivery-1"], agents["Delivery-2"]
    task = await h.tasks.update(task.id, status="In-Progress", assigned_agent_id=native.id)
    swarm = await h.swarm()
    await h.engine.jobs._reassign_if_incapable(swarm, team, task)
    after = await h.tasks.get(task.id)
    assert after.assigned_agent_id == remote_agent.id
    evs = [e for e in await h.event_list("info") if e.task_id == task.id]
    assert evs and "reassigned to Delivery-2" in evs[0].detail
    # nobody capable and idle: stays put
    await h.agents.set_state(remote_agent.id, "busy")
    await h.tasks.update(task.id, assigned_agent_id=native.id)
    await h.engine.jobs._reassign_if_incapable(swarm, team, await h.tasks.get(task.id))
    assert (await h.tasks.get(task.id)).assigned_agent_id == native.id


async def test_lease_requires_the_jobs_capabilities(h, api, pool):
    await make_remote_job(h, pool, required=("run_code",))
    edit_only = FakeRunner(api, (await new_runner(api, "edit"))[1], "edit",
                           harnesses=[{"harness": "fake", "models": [],
                                       "capabilities": ["edit_files"]}])
    full = FakeRunner(api, (await new_runner(api, "full"))[1], "full",
                      harnesses=[{"harness": "fake", "models": [],
                                  "capabilities": ["edit_files", "run_code"]}])
    legacy = FakeRunner(api, (await new_runner(api, "legacy"))[1], "legacy",
                        harnesses=[{"harness": "fake", "models": []}])
    for r in (edit_only, full, legacy):
        assert (await r.hello()).status_code == 200
    assert await edit_only.lease() is None
    job = await full.lease()
    assert job and job["required_capabilities"] == ["run_code"]
    # a job needing nothing goes to anyone, an old hello means the harness default (run_code)
    await make_remote_job(h, pool, task_name="Plain", artifact="p.md")
    assert (await edit_only.lease()) is not None
    await make_remote_job(h, pool, task_name="Needs", artifact="n.md", required=("run_code",))
    assert (await legacy.lease())["required_capabilities"] == ["run_code"]
