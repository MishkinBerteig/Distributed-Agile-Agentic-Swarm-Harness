"""Command checks against real PostgreSQL (docs/DESIGN-0.3.md section 12).

The in-process FakeRunner really runs the job's `checks` (through daash_runner.checks, in a
scratch directory holding the delivered files), so the command results are genuine."""
from __future__ import annotations

import pytest

from app.models import CheckKind, CriterionCreate
from tests.integration.helpers import default_criteria, swarm_config
from tests.integration.runner_helpers import FakeRunner  # noqa: F401 - documented dependency
from tests.integration.test_capabilities import cfg_for
from tests.integration.test_engine_remote import VISION, jobs_of, runner, stop_engine

PASS_CMD = "python3 -c \"print('3 tests OK')\""
FAIL_CMD = "python3 -c \"import sys; print('boom: 1 test failed'); sys.exit(3)\""


@pytest.fixture(autouse=True)
def fast_poll(h):
    h.engine.jobs.remote_poll_seconds = 0.02


def command_criteria(artifact: str, command: str) -> list[CriterionCreate]:
    return default_criteria(artifact) + [CriterionCreate(
        text=f"`{command}` exits 0", check_kind=CheckKind.COMMAND, check_arg=command)]


async def ready_command_task(h, harnesses, command):
    await h.create_swarm(VISION, config=cfg_for(harnesses), dod="- The guide exists")
    team = await h.seed_team("Team A")
    task = await h.add_task(team, "Verify", status="Ready", complexity="moderate",
                            artifact="verify.md", criteria=command_criteria("verify.md", command))
    await h.transition("start")
    return team, task


async def _status(h, task, status):
    return (await h.tasks.get(task.id)).status == status


async def _feedback_has(h, task, text):
    return text in ((await h.tasks.get(task.id)).feedback or "")


def command_criterion(task):
    return next(c for c in task.criteria if c.check_kind == "command")


async def test_task_with_command_criterion_requires_run_code(h, api):
    await h.create_swarm(VISION, config=cfg_for(["daash"]), dod="- d")
    team = await h.seed_team("Team A")
    r = await api.post("/tasks", json={
        "team_id": team.id, "name": "Tests", "acceptance_criteria": [
            {"text": "tests pass", "check_kind": "command", "check_arg": "python3 -m unittest"}]})
    assert r.status_code == 201, r.text
    assert r.json()["required_capabilities"] == ["run_code"]
    bad = await api.post("/tasks", json={
        "team_id": team.id, "name": "Bad", "acceptance_criteria": [
            {"text": "x", "check_kind": "command", "check_arg": "a\nb"}]})
    assert bad.status_code == 422 or bad.status_code == 400, bad.text
    empty = await api.post("/tasks", json={
        "team_id": team.id, "name": "Empty", "acceptance_criteria": [
            {"text": "x", "check_kind": "command"}]})
    assert empty.status_code in (400, 422), empty.text


async def test_passing_command_goes_green_and_task_done(h, api, pool, fake):
    fake.configure({"record_prompts": True})
    team, task = await ready_command_task(h, ["daash", "fake"], PASS_CMD)
    assert (await h.tasks.get(task.id)).required_capabilities == ["run_code"]   # automatic
    fr = (await runner(api)).start()
    try:
        await h.run_until(lambda: _status(h, task, "Done"), label="task done", max_steps=300)
    finally:
        await fr.close()
        await stop_engine(h)
    done = await h.tasks.get(task.id)
    crit = command_criterion(done)
    assert crit.status == "green" and crit.evidence.startswith("exit 0")
    assert "3 tests OK" in crit.evidence
    (job,) = await jobs_of(pool, task.id)
    assert job.payload["checks"] == [{"id": crit.id, "command": PASS_CMD, "timeout_seconds": 120}]
    assert job.result["check_results"][0]["exit_code"] == 0
    assert done.deliverable["check_results"][0]["id"] == crit.id
    # the judges saw the result as DAASH-produced evidence
    q = h.calls_of("review_quality")[0]["user"]
    assert "automatically checked by DAASH's runner, not by the agent" in q and "3 tests OK" in q


async def test_failing_command_is_red_and_feeds_rework(h, api):
    team, task = await ready_command_task(h, ["daash", "fake"], FAIL_CMD)
    fr = (await runner(api)).start()
    try:
        await h.run_until(lambda: _feedback_has(h, task, "boom: 1 test failed"),
                          label="rework feedback has the command output", max_steps=300)
    finally:
        await fr.close()
        await stop_engine(h)
    t = await h.tasks.get(task.id)
    assert t.status != "Done"
    crit = command_criterion(t)
    assert crit.status == "red" and "exit 3" in crit.evidence


async def test_native_work_has_no_runner_so_command_is_red(h):
    # Only a native agent exists: the task is delivered degraded, nothing runs the command.
    team, task = await ready_command_task(h, ["daash"], PASS_CMD)
    try:
        await h.run_until(lambda: _feedback_has(h, task, "no runner executed this check"),
                          label="red: no runner executed", max_steps=300)
    finally:
        await stop_engine(h)
    t = await h.tasks.get(task.id)
    assert t.status != "Done"
    assert command_criterion(t).status == "red"


async def test_plan_emits_command_criterion_and_run_code(h, fake):
    fake.configure({"plan_command_check": "python3 -m unittest", "tasks_per_plan": 2})
    await h.create_swarm(VISION, config=cfg_for(["daash", "fake"]), dod="- d")
    await h.seed_team("Team A")
    await h.transition("start")

    async def planned():
        return bool(await h.task_list())
    await h.run_until(planned, label="planned")
    await stop_engine(h)
    tasks = await h.task_list()
    first, second = tasks[0], tasks[1]
    crit = command_criterion(first)
    assert crit.check_arg == "python3 -m unittest" and crit.check_path is None
    assert first.required_capabilities == ["run_code"]
    assert not [c for c in second.criteria if c.check_kind == "command"]
    assert second.required_capabilities == []
    _ = swarm_config
