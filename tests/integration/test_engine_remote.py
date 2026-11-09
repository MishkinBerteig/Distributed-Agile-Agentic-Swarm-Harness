"""The engine with remote delivery agents, against an in-process fake runner
(docs/DESIGN-0.3.md section 6 and 9). The runner talks to the real API over ASGI."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.models import HarnessSpec
from app.repo import RemoteJobRepo
from tests.integration.helpers import swarm_config
from tests.integration.runner_helpers import FakeRunner, new_runner

VISION = "Write a short guide to compost."


@pytest.fixture(autouse=True)
def fast_poll(h):
    h.engine.jobs.remote_poll_seconds = 0.02


def remote_config(delivery: int = 1, harnesses=("fake",), **kw):
    cfg = swarm_config(delivery, **kw)
    cfg.allowed_harnesses = ["daash", "fake"]
    cfg.delivery_harnesses = [HarnessSpec(harness=x, model="") for x in harnesses]
    return cfg


async def seeded_task(h, config, name="Remote task", artifact="remote.md"):
    """A started swarm, one team and one In-Progress task owned by Delivery-1."""
    await h.create_swarm(VISION, config=config, dod="- The guide exists")
    team = await h.seed_team("Team A")
    agent = next(a for a in await h.agent_list(team) if a.name == "Delivery-1")
    task = await h.add_task(team, name, artifact=artifact, status="In-Progress",
                            assigned_agent_id=agent.id, complexity="moderate")
    await h.transition("start")
    return team, agent, task


async def runner(api, name="fake-runner", **kw) -> FakeRunner:
    _, token = await new_runner(api, name)
    return FakeRunner(api, token, name, **kw)


async def jobs_of(pool, task_id):
    rows = await pool.fetch("SELECT id FROM remote_jobs WHERE task_id = $1 ORDER BY created_at",
                            task_id)
    return [await RemoteJobRepo(pool).get(r["id"]) for r in rows]


async def tick_until(h, pred, label, timeout=15.0):
    """Tick the engine without waiting for its jobs until `pred()` holds."""
    async def check():
        await h.engine.tick()
        return await pred()
    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, f"timed out: {label}\n{await h.dump()}"
        await asyncio.sleep(0.02)


async def stop_engine(h):
    """Cancel whatever the engine still runs (a remote job it re-queued, for instance)."""
    for t in list(h.engine.running.values()):
        t.cancel()
    await asyncio.gather(*h.engine.running.values(), return_exceptions=True)
    h.engine.running.clear()


async def task_status(h, task):
    return (await h.tasks.get(task.id)).status


# -- mixed team ---------------------------------------------------------------------------------

async def test_mixed_team_native_and_remote_reaches_verification(h, api, fake, pool):
    fake.configure({"tasks_per_plan": 2})
    await h.create_swarm(VISION, config=remote_config(2, ("daash", "fake")),
                         dod="- The guide exists")
    await h.seed_team("Team A")
    await h.transition("start")
    agents = [a for a in await h.agent_list() if a.role == "mission_delivery"]
    assert sorted((a.name, a.harness) for a in agents) == [("Delivery-1", "daash"),
                                                           ("Delivery-2", "fake")]
    fr = (await runner(api)).start()
    try:
        await h.run_to_state("VERIFICATION")
    finally:
        await fr.close()
    tasks = await h.task_list()
    assert [t.status for t in tasks] == ["Done", "Done"]
    by_harness = {(t.deliverable.get("harness") or "daash"): t for t in tasks}
    assert set(by_harness) == {"daash", "fake"}
    remote_task = by_harness["fake"]
    assert remote_task.deliverable["runner"] == "fake-runner"
    assert "Overview" in h.workspace_file(remote_task.artifact)
    (job,) = await jobs_of(pool, remote_task.id)
    assert job.status == "completed" and job.harness == "fake" and job.attempt == 0
    assert job.model == remote_task.deliverable["model"]
    assert not (Path(h.settings.WORKSPACE_ROOT) / h.swarm_id / ".daash" / "staging" / job.id).exists()
    submitted = [e for e in await h.event_list("submitted") if e.task_id == remote_task.id]
    assert "fake on runner 'fake-runner'" in submitted[0].detail
    calls = [c for c in await h.calls.list_for_swarm(h.swarm_id) if c.task_id == remote_task.id
             and c.purpose == "work_task"]
    assert len(calls) == 1 and calls[0].total_tokens == 30 and calls[0].ok
    assert all(a.state == "idle" for a in await h.agent_list())
    assert not await h.event_list("error")
    assert fr.workspaces[job.id] is not None


async def test_remote_job_carries_prompt_and_limits(h, api, pool):
    team, agent, task = await seeded_task(h, remote_config(), artifact="guides/basil.md")
    await tick_until(h, lambda: _has_jobs(pool, task.id), "job queued")
    (job,) = await jobs_of(pool, task.id)
    assert job.status == "queued" and job.agent_id == agent.id and job.harness == "fake"
    p = job.payload
    assert "Vision" in p["system_prompt"] and "Delivery" in p["system_prompt"]
    assert "guides/basil.md" in p["prompt"] and "Acceptance criteria" in p["prompt"]
    assert p["limits"]["timeout_seconds"] == 30 * 60 and p["task"]["artifact"] == "guides/basil.md"
    assert (await h.agents.get(agent.id)).state == "busy"
    await stop_engine(h)


async def _has_jobs(pool, task_id):
    return bool(await jobs_of(pool, task_id))


# -- restart ------------------------------------------------------------------------------------

async def test_engine_restart_reattaches_to_the_same_job(h, api, pool, monkeypatch):
    team, agent, task = await seeded_task(h, remote_config())
    fr = await runner(api)
    await tick_until(h, lambda: _has_jobs(pool, task.id), "job queued")
    await fr.hello()
    job = await fr.lease()
    await fr.workspace(job)
    (before,) = await jobs_of(pool, task.id)
    assert before.status == "leased"

    # "kill -9" the engine: its tasks end without the graceful job cancellation
    async def no_cancel(self, job_id):
        return None
    with monkeypatch.context() as m:
        m.setattr(RemoteJobRepo, "cancel", no_cancel)
        await stop_engine(h)
    await h.agents.reset_busy()

    engine2 = h.new_engine()
    engine2.jobs.remote_poll_seconds = 0.02
    try:
        await engine2.tick()
        assert engine2.running
        await asyncio.sleep(0.2)
        assert len(await jobs_of(pool, task.id)) == 1               # no duplicate
        assert await fr.post_result(job) == 200                    # the runner finishes anyway
        await h.wait_for(lambda: _status_is(h, task, "Review"), label="Review")
    finally:
        for t in list(engine2.running.values()):
            t.cancel()
        await asyncio.gather(*engine2.running.values(), return_exceptions=True)
    (after,) = await jobs_of(pool, task.id)
    assert after.id == before.id and after.status == "completed"
    assert "Overview" in h.workspace_file(task.artifact)


async def _status_is(h, task, status):
    return (await h.tasks.get(task.id)).status == status


# -- pause --------------------------------------------------------------------------------------

async def test_pause_cancels_the_remote_job_and_heartbeat_says_cancelled(h, api, pool):
    team, agent, task = await seeded_task(h, remote_config())
    fr = (await runner(api, behaviors=["hang"])).start()
    try:
        await tick_until(h, lambda: _leased(pool, task.id), "job leased")
        await h.transition("pause")
        await h.engine.tick()                                      # cancels the stale job
        await h.settle()
        (job,) = await jobs_of(pool, task.id)
        assert job.status == "cancelled"
        await h.wait_for(lambda: _cancel_seen(fr), label="runner told to stop")
        assert fr.cancelled == [job.id]
    finally:
        await fr.close()
    t = await h.tasks.get(task.id)
    assert t.status == "In-Progress" and t.attempts == 0
    assert (await h.agents.get(agent.id)).state == "idle"
    assert await fr.post_result({"id": job.id, "task": {"name": "t", "artifact": ""}}) == 409
    assert not await h.event_list("error")


async def _leased(pool, task_id):
    return any(j.status == "leased" for j in await jobs_of(pool, task_id))


async def _cancel_seen(fr):
    return bool(fr.cancelled)


# -- lease expiry -------------------------------------------------------------------------------

async def test_lease_expiry_requeues_and_second_runner_completes(h, api, pool):
    team, agent, task = await seeded_task(h, remote_config())
    a = await runner(api, "runner-a")
    await a.hello()
    await tick_until(h, lambda: _has_jobs(pool, task.id), "job queued")
    job_a = await a.lease()
    await pool.execute("UPDATE remote_jobs SET lease_expires_at = now() - interval '1 minute'")
    await tick_until(h, lambda: _requeued(pool, task.id), "job requeued")
    b = (await runner(api, "runner-b")).start()
    try:
        await tick_until(h, lambda: _status_is(h, task, "Review"), "Review")
        await h.settle()
    finally:
        await b.close()
    (job,) = await jobs_of(pool, task.id)
    assert job.id == job_a["id"] and job.expiries == 1 and job.status == "completed"
    t = await h.tasks.get(task.id)
    assert t.deliverable["runner"] == "runner-b"
    assert await a.post_result(job_a) == 409                       # the vanished runner is too late


async def _requeued(pool, task_id):
    return any(j.status == "queued" and j.expiries == 1 for j in await jobs_of(pool, task_id))


async def test_runner_that_keeps_vanishing_is_an_infrastructure_error(h, api, pool):
    team, agent, task = await seeded_task(h, remote_config())
    await tick_until(h, lambda: _has_jobs(pool, task.id), "job queued")
    await pool.execute("UPDATE remote_jobs SET expiries = 3")
    await tick_until(h, lambda: _abandoned(pool, task.id), "job abandoned")
    await h.settle()
    t = await h.tasks.get(task.id)
    assert t.attempts == 0 and t.status == "In-Progress"
    assert any("vanished" in e.detail for e in await h.event_list("error"))
    n = len(await jobs_of(pool, task.id))
    await h.engine.tick()
    assert len(await jobs_of(pool, task.id)) == n                  # cooldown holds it back


async def _abandoned(pool, task_id):
    return any(j.status == "abandoned" for j in await jobs_of(pool, task_id))


# -- error / failed -------------------------------------------------------------------------------

async def test_error_outcome_cools_down_and_does_not_count_an_attempt(h, api, pool):
    team, agent, task = await seeded_task(h, remote_config())
    fr = (await runner(api, behaviors=["error", "ok"])).start()
    try:
        await tick_until(h, lambda: _errored(h), "error event")
        await h.settle()
        t = await h.tasks.get(task.id)
        assert t.attempts == 0 and t.status == "In-Progress"
        (first,) = await jobs_of(pool, task.id)
        assert first.status == "completed" and first.result["outcome"] == "error"
        assert any("remote runner" in e.detail for e in await h.event_list("error"))
        calls = [c for c in await h.calls.list_for_swarm(h.swarm_id) if c.purpose == "work_task"]
        assert len(calls) == 1 and calls[0].ok is False
        await h.engine.tick()
        await asyncio.sleep(0.2)
        assert len(await jobs_of(pool, task.id)) == 1               # still cooling down
        h.clock.advance(10_000)
        await tick_until(h, lambda: _status_is(h, task, "Review"), "Review after cooldown")
        await h.settle()
    finally:
        await fr.close()
    jobs = await jobs_of(pool, task.id)
    assert len(jobs) == 2 and {j.attempt for j in jobs} == {0}
    assert (await h.agents.get(agent.id)).errors == 1


async def _errored(h):
    return bool(await h.event_list("error"))


async def test_failed_outcome_counts_an_attempt_and_reworks(h, api, pool):
    team, agent, task = await seeded_task(h, remote_config())
    fr = (await runner(api, behaviors=["failed", "ok"])).start()
    try:
        await tick_until(h, lambda: _status_is(h, task, "Review"), "Review after rework")
        await h.settle()
    finally:
        await fr.close()
    jobs = await jobs_of(pool, task.id)
    assert [j.attempt for j in jobs] == [0, 1]
    assert jobs[0].result["outcome"] == "failed"
    rework = await h.event_list("rework")
    assert rework and "failed (scripted)" in rework[0].detail
    assert not await h.event_list("error")


async def test_failed_until_attempts_exhausted_fails_the_task(h, api, pool):
    team, agent, task = await seeded_task(h, remote_config(max_attempts_per_task=2))
    fr = (await runner(api, behaviors=["failed"])).start()
    try:
        await tick_until(h, lambda: _status_is(h, task, "Failed"), "Failed")
        await h.settle()
    finally:
        await fr.close()
    assert (await h.tasks.get(task.id)).attempts == 2


async def test_leased_too_long_fails_the_attempt(h, api, pool):
    team, agent, task = await seeded_task(h, remote_config())
    fr = (await runner(api, behaviors=["vanish"])).start()
    try:
        await tick_until(h, lambda: _leased(pool, task.id), "job leased")
        await pool.execute("UPDATE remote_jobs SET leased_at = now() - interval '2 hours', "
                           "lease_expires_at = now() + interval '1 hour'")
        await tick_until(h, lambda: _rework_event(h), "timeout rework")
    finally:
        await fr.close()
        await stop_engine(h)
    t = await h.tasks.get(task.id)
    assert t.attempts == 1
    assert "ran out of time" in (await h.event_list("rework"))[0].detail
    assert (await jobs_of(pool, task.id))[0].status == "cancelled"


async def _rework_event(h):
    return bool(await h.event_list("rework"))


# -- fallback -----------------------------------------------------------------------------------

async def test_no_runner_falls_back_to_the_native_harness(h, api, fake, pool):
    cfg = remote_config()
    cfg.remote_fallback_minutes = 1
    team, agent, task = await seeded_task(h, cfg)
    await tick_until(h, lambda: _has_jobs(pool, task.id), "job queued")
    await asyncio.sleep(0.1)
    assert await task_status(h, task) == "In-Progress"             # still waiting for a runner
    await pool.execute("UPDATE remote_jobs SET created_at = now() - interval '5 minutes'")
    await tick_until(h, lambda: _status_is(h, task, "Review"), "native Review")
    await h.settle()
    (job,) = await jobs_of(pool, task.id)
    assert job.status == "abandoned"
    t = await h.tasks.get(task.id)
    assert "harness" not in t.deliverable and t.deliverable["files"] == [task.artifact]
    infos = [e for e in await h.event_list("info") if "built-in harness" in e.detail]
    assert infos and "Delivery-1" in infos[0].detail and "'fake'" in infos[0].detail
    assert fake.calls_for("work")                                  # the native loop ran


async def test_no_fallback_when_disabled(h, api, pool):
    cfg = remote_config()
    cfg.remote_fallback_minutes = None
    team, agent, task = await seeded_task(h, cfg)
    await tick_until(h, lambda: _has_jobs(pool, task.id), "job queued")
    await pool.execute("UPDATE remote_jobs SET created_at = now() - interval '5 days'")
    await asyncio.sleep(0.2)
    assert (await jobs_of(pool, task.id))[0].status == "queued"
    await stop_engine(h)
    assert (await jobs_of(pool, task.id))[0].status == "cancelled"  # engine stop cancels it


# -- misc ----------------------------------------------------------------------------------------

async def test_deleted_paths_stay_inside_the_workspace(h, api, pool):
    team, agent, task = await seeded_task(h, remote_config())
    ws = Path(h.settings.WORKSPACE_ROOT) / h.swarm_id
    (ws / "old.md").write_text("old")
    outside = ws.parent / "outside.txt"
    outside.write_text("keep me")
    _, token = await new_runner(api, "r")
    fr = FakeRunner(api, token, "r")
    await fr.hello()
    await tick_until(h, lambda: _has_jobs(pool, task.id), "job queued")
    job = await fr.lease()
    ok = await api.post(f"/runner/jobs/{job['id']}/result", headers=fr.headers, json={
        "outcome": "submitted", "summary": "s", "deleted": ["old.md", "missing.md"],
        "files": [{"path": task.artifact, "content_b64": "T3ZlcnZpZXc="}]})
    assert ok.status_code == 200
    bad = await api.post(f"/runner/jobs/{job['id']}/result", headers=fr.headers, json={
        "outcome": "submitted", "deleted": ["../outside.txt"]})
    assert bad.status_code == 409                                  # already completed
    await tick_until(h, lambda: _status_is(h, task, "Review"), "Review")
    await h.settle()
    assert not (ws / "old.md").exists() and outside.read_text() == "keep me"
    assert (ws / task.artifact).read_text() == "Overview"
