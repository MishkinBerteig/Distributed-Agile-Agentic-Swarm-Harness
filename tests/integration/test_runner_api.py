"""Runner and admin endpoints, auth, leasing and result validation (docs/DESIGN-0.3.md 4 and 5)."""
from __future__ import annotations

import asyncio
import io
import json
import tarfile
import time
from pathlib import Path

import pytest

from app import main, remote
from app.repo import RemoteJobRepo, RunnerRepo
from tests.integration.runner_helpers import FakeRunner, b64, make_remote_job, new_runner

ADMIN = "admin-secret-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(main, "LEASE_POLL_SECONDS", 0.05)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def runner_with(api, name="r1", harnesses=None, max_jobs=4):
    runner, token = await new_runner(api, name)
    fr = FakeRunner(api, token, name, harnesses=harnesses, max_jobs=max_jobs)
    assert (await fr.hello()).status_code == 200
    return runner, fr


# -- admin management ---------------------------------------------------------------------------

async def test_create_list_patch_delete_runner(api):
    r = await api.post("/runners", json={"name": "mac-mini"})
    assert r.status_code == 201
    body = r.json()
    token = body["token"]
    assert token.startswith("daash_rt_") and len(token) == len("daash_rt_") + 43
    runner = body["runner"]
    assert runner["name"] == "mac-mini" and runner["enabled"] and not runner["online"]
    assert runner["active_jobs"] == 0 and runner["harnesses"] == []
    assert "token_hash" not in runner and token not in json.dumps(runner)

    assert (await api.post("/runners", json={"name": "mac-mini"})).status_code == 409
    assert (await api.post("/runners", json={"name": "  "})).status_code == 422
    assert (await api.post("/runners", json={})).status_code == 422

    listed = (await api.get("/runners")).json()["runners"]
    assert [x["id"] for x in listed] == [runner["id"]]
    assert "token_hash" not in listed[0]

    p = await api.patch(f"/runners/{runner['id']}", json={"enabled": False})
    assert p.status_code == 200 and p.json()["enabled"] is False
    assert (await api.patch("/runners/nope", json={"enabled": True})).status_code == 404
    assert (await api.patch(f"/runners/{runner['id']}", json={})).status_code == 422

    assert (await api.delete(f"/runners/{runner['id']}")).status_code == 204
    assert (await api.delete(f"/runners/{runner['id']}")).status_code == 404
    assert (await api.get("/runners")).json()["runners"] == []


async def test_token_is_stored_hashed(api, pool):
    _, token = await new_runner(api, "hashed")
    stored = await pool.fetchval("SELECT token_hash FROM runners WHERE name = 'hashed'")
    assert stored == remote.hash_token(token) and stored != token


# -- auth ---------------------------------------------------------------------------------------

async def test_auth_off_everything_open(api):
    h = (await api.get("/health")).json()
    assert h == {"status": "ok", "auth_required": False}
    assert (await api.get("/swarms")).status_code == 200


async def test_auth_on_admin_endpoints_need_bearer(api, settings):
    settings.API_TOKEN = ADMIN
    assert (await api.get("/health")).json() == {"status": "ok", "auth_required": True}
    for method, path in (("get", "/swarms"), ("get", "/config"), ("get", "/runners"),
                         ("get", "/remote-jobs"), ("post", "/runners")):
        r = await getattr(api, method)(path)
        assert r.status_code == 401, path
        assert r.json() == {"detail": "Authentication required"}
    assert (await api.get("/swarms", headers=bearer("wrong"))).status_code == 401
    assert (await api.get("/swarms", headers={"Authorization": f"Basic {ADMIN}"})).status_code == 401
    assert (await api.get("/swarms", headers=AUTH)).status_code == 200
    created = await api.post("/runners", json={"name": "x"}, headers=AUTH)
    assert created.status_code == 201


async def test_runner_endpoints_use_runner_tokens_not_admin_token(api, settings):
    settings.API_TOKEN = ADMIN
    _, token = await new_runner(api, "r1", admin=AUTH)
    hello = {"host": "h", "version": "1", "max_jobs": 1, "harnesses": []}
    assert (await api.post("/runner/hello", json=hello, headers=bearer(token))).status_code == 200
    assert (await api.post("/runner/hello", json=hello, headers=AUTH)).status_code == 401
    assert (await api.post("/runner/hello", json=hello)).status_code == 401
    assert (await api.post("/runner/hello", json=hello,
                           headers=bearer("daash_rt_unknown"))).status_code == 401


async def test_disabled_runner_gets_401_everywhere(api):
    runner, fr = await runner_with(api)
    await api.patch(f"/runners/{runner['id']}", json={"enabled": False})
    assert (await fr.hello()).status_code == 401
    r = await api.post("/runner/lease", headers=fr.headers, json={"wait_seconds": 0})
    assert r.status_code == 401
    assert (await api.get("/runner/jobs/x/workspace", headers=fr.headers)).status_code == 401
    assert (await api.post("/runner/jobs/x/heartbeat", headers=fr.headers, json={})).status_code == 401
    assert (await api.post("/runner/jobs/x/result", headers=fr.headers, json={})).status_code == 401
    await api.patch(f"/runners/{runner['id']}", json={"enabled": True})
    assert (await fr.hello()).status_code == 200


async def test_bootstrap_runner(api, pool):
    repo = RunnerRepo(pool)
    first = await repo.ensure_bootstrap(main.BOOTSTRAP_RUNNER, remote.hash_token("boot-1"))
    assert first.name == "local-runner"
    again = await repo.ensure_bootstrap(main.BOOTSTRAP_RUNNER, remote.hash_token("boot-1"))
    assert again.id == first.id
    hello = {"host": "h", "version": "1", "max_jobs": 1, "harnesses": []}
    assert (await api.post("/runner/hello", json=hello, headers=bearer("boot-1"))).status_code == 200
    rotated = await repo.ensure_bootstrap(main.BOOTSTRAP_RUNNER, remote.hash_token("boot-2"))
    assert rotated.id == first.id
    assert (await api.post("/runner/hello", json=hello, headers=bearer("boot-1"))).status_code == 401
    assert (await api.post("/runner/hello", json=hello, headers=bearer("boot-2"))).status_code == 200


async def test_lifespan_creates_bootstrap_runner(pool, bus, settings, monkeypatch):
    monkeypatch.setattr(main, "settings", settings)
    settings.RUNNER_BOOTSTRAP_TOKEN = "boot-from-env"
    async with main.lifespan(main.app):
        runner = await RunnerRepo(main.pool).get_by_name("local-runner")
        assert runner is not None and runner.token_hash == remote.hash_token("boot-from-env")


# -- hello --------------------------------------------------------------------------------------

async def test_hello_stores_capabilities(api):
    runner, token = await new_runner(api, "r1")
    r = await api.post("/runner/hello", headers=bearer(token), json={
        "host": "mini", "version": "0.3", "max_jobs": 2,
        "harnesses": [{"harness": "codex", "models": ["fast"]}, {"harness": "bogus"},
                      {"harness": "fake", "models": []}]})
    assert r.status_code == 200
    assert r.json() == {"runner_id": runner["id"], "name": "r1", "poll_seconds": 5,
                        "heartbeat_seconds": 15, "lease_seconds": 60}
    (listed,) = (await api.get("/runners")).json()["runners"]
    assert listed["host"] == "mini" and listed["version"] == "0.3" and listed["max_jobs"] == 2
    both = ["edit_files", "run_code"]
    assert listed["harnesses"] == [{"harness": "codex", "models": ["fast"], "capabilities": both},
                                   {"harness": "fake", "models": [], "capabilities": both}]
    assert listed["online"] is True and listed["last_seen_at"]
    bad = await api.post("/runner/hello", headers=bearer(token), json={"max_jobs": 0})
    assert bad.status_code == 422


# -- lease --------------------------------------------------------------------------------------

async def test_lease_204_cases(api, h, pool):
    runner, fr = await runner_with(api)
    r = await api.post("/runner/lease", headers=fr.headers, json={"wait_seconds": 0})
    assert r.status_code == 204                                   # nothing queued
    await make_remote_job(h, pool)
    r = await api.post("/runner/lease", headers=fr.headers,
                       json={"wait_seconds": 5, "free_slots": 0})
    assert r.status_code == 204                                   # no free slot: immediately
    assert (await api.post("/runner/lease", headers=fr.headers,
                           json={"wait_seconds": 31})).status_code == 422
    assert (await api.post("/runner/lease", headers=fr.headers,
                           json={"wait_seconds": -1})).status_code == 422


async def test_long_poll_waits_then_204(api):
    _, fr = await runner_with(api)
    t0 = time.monotonic()
    r = await api.post("/runner/lease", headers=fr.headers, json={"wait_seconds": 1})
    assert r.status_code == 204 and 0.9 <= time.monotonic() - t0 < 3


async def test_long_poll_returns_job_that_arrives_meanwhile(api, h, pool):
    await h.create_swarm(dod="- The guide exists")
    await h.seed_team("Team A")
    await h.transition("start")
    _, fr = await runner_with(api)
    poll = asyncio.create_task(fr.lease(wait=5))
    await asyncio.sleep(0.3)
    assert not poll.done()
    job, _ = await make_remote_job(h, pool)
    got = await asyncio.wait_for(poll, 3)
    assert got["id"] == job.id


async def test_lease_returns_the_job_payload(api, h, pool):
    job, task = await make_remote_job(h, pool, model="fast", artifact="guides/basil.md")
    runner, fr = await runner_with(api)
    got = await fr.lease()
    assert got["id"] == job.id and got["task_id"] == task.id and got["swarm_id"] == task.swarm_id
    assert got["harness"] == "fake" and got["model"] == "fast" and got["attempt"] == 0
    assert got["task"] == {"name": task.name, "artifact": "guides/basil.md"}
    assert got["system_prompt"] == "sys" and got["prompt"] == "do it"
    assert got["limits"]["timeout_seconds"] == 1800
    assert got["workspace_url"] == f"/runner/jobs/{job.id}/workspace"
    row = await RemoteJobRepo(pool).get(job.id)
    assert row.status == "leased" and row.runner_id == runner["id"] and row.leased_at
    assert row.lease_expires_at > row.leased_at
    (listed,) = (await api.get("/runners")).json()["runners"]
    assert listed["active_jobs"] == 1
    assert await fr.lease() is None                               # already leased


async def test_lease_matching_harness_and_model(api, h, pool):
    await make_remote_job(h, pool, harness="codex", model="great")
    _, other = await runner_with(api, "other", [{"harness": "fake", "models": []}])
    _, wrong_model = await runner_with(api, "wm", [{"harness": "codex", "models": ["fast"]}])
    _, right = await runner_with(api, "right", [{"harness": "codex", "models": ["great", "fast"]}])
    assert await other.lease() is None
    assert await wrong_model.lease() is None
    assert (await right.lease())["harness"] == "codex"


async def test_lease_skips_jobs_of_swarms_not_active(api, h, pool):
    await make_remote_job(h, pool)
    _, fr = await runner_with(api)
    await h.transition("pause")
    assert await fr.lease() is None
    await h.transition("resume")
    assert await fr.lease() is not None


async def test_lease_oldest_first_and_respects_max_jobs(api, h, pool):
    j1, _ = await make_remote_job(h, pool, task_name="One", artifact="one.md")
    team = (await h.team_list())[0]
    j2, _ = await make_remote_job(h, pool, task_name="Two", artifact="two.md", team=team)
    _, fr = await runner_with(api, max_jobs=1)
    assert (await fr.lease())["id"] == j1.id
    assert await fr.lease() is None                               # at its max_jobs
    _, fr2 = await runner_with(api, "r2", max_jobs=1)
    assert (await fr2.lease())["id"] == j2.id


async def test_concurrent_runners_never_share_a_job(api, h, pool):
    await make_remote_job(h, pool)
    runners = [(await runner_with(api, f"r{i}"))[1] for i in range(4)]
    results = await asyncio.gather(*(r.lease() for r in runners))
    assert len([r for r in results if r]) == 1


async def test_expired_lease_is_requeued_for_another_runner(api, h, pool):
    job, _ = await make_remote_job(h, pool)
    _, a = await runner_with(api, "a")
    _, b = await runner_with(api, "b")
    assert (await a.lease())["id"] == job.id
    await pool.execute("UPDATE remote_jobs SET lease_expires_at = now() - interval '1 minute'")
    got = await b.lease()
    assert got["id"] == job.id
    row = await RemoteJobRepo(pool).get(job.id)
    assert row.expiries == 1 and row.status == "leased"
    # the first runner lost the job
    assert (await api.post(f"/runner/jobs/{job.id}/heartbeat", headers=a.headers,
                           json={})).status_code == 404
    assert await a.post_result(got) == 409


async def test_deleting_a_runner_requeues_its_jobs(api, h, pool):
    job, _ = await make_remote_job(h, pool)
    runner, a = await runner_with(api, "a")
    await a.lease()
    assert (await api.delete(f"/runners/{runner['id']}")).status_code == 204
    row = await RemoteJobRepo(pool).get(job.id)
    assert row.status == "queued" and row.runner_id is None


# -- workspace ---------------------------------------------------------------------------------

async def test_workspace_tarball(api, h, pool, settings):
    job, task = await make_remote_job(h, pool)
    ws = Path(settings.WORKSPACE_ROOT) / task.swarm_id
    (ws / "sub").mkdir(parents=True, exist_ok=True)
    (ws / "sub" / "a.md").write_text("alpha")
    (ws / ".daash" / "staging").mkdir(parents=True)
    (ws / ".daash" / "staging" / "x").write_text("hidden")
    _, fr = await runner_with(api)
    _, other = await runner_with(api, "other")
    url = f"/runner/jobs/{job.id}/workspace"
    assert (await api.get(url, headers=fr.headers)).status_code == 404       # not leased yet
    got = await fr.lease()
    r = await api.get(got["workspace_url"], headers=fr.headers)
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        assert tar.getnames() == ["sub/a.md"]
        assert tar.extractfile("sub/a.md").read() == b"alpha"
    assert (await api.get(url, headers=other.headers)).status_code == 404    # someone else's
    assert (await api.get("/runner/jobs/nope/workspace", headers=fr.headers)).status_code == 404


# -- heartbeat ---------------------------------------------------------------------------------

async def test_heartbeat_extends_lease(api, h, pool):
    job, _ = await make_remote_job(h, pool)
    _, fr = await runner_with(api)
    await fr.lease()
    await pool.execute("UPDATE remote_jobs SET lease_expires_at = now() + interval '1 second'")
    r = await api.post(f"/runner/jobs/{job.id}/heartbeat", headers=fr.headers,
                       json={"note": "working"})
    assert r.status_code == 200 and r.json() == {"status": "leased"}
    row = await RemoteJobRepo(pool).get(job.id)
    assert (row.lease_expires_at - row.heartbeat_at).total_seconds() == pytest.approx(60, abs=1)
    assert (await api.post(f"/runner/jobs/{job.id}/heartbeat", headers=fr.headers)
            ).json() == {"status": "leased"}                       # body is optional


async def test_heartbeat_404_for_other_runner_or_unknown(api, h, pool):
    job, _ = await make_remote_job(h, pool)
    _, a = await runner_with(api, "a")
    _, b = await runner_with(api, "b")
    await a.lease()
    for fr, jid in ((b, job.id), (a, "nope")):
        r = await api.post(f"/runner/jobs/{jid}/heartbeat", headers=fr.headers, json={})
        assert r.status_code == 404


async def test_heartbeat_says_cancelled_when_swarm_paused(api, h, pool):
    job, _ = await make_remote_job(h, pool)
    _, fr = await runner_with(api)
    await fr.lease()
    await h.transition("pause")
    assert await fr.heartbeat({"id": job.id}) == "cancelled"
    assert (await RemoteJobRepo(pool).get(job.id)).status == "cancelled"
    assert await fr.heartbeat({"id": job.id}) == "cancelled"        # and stays so
    assert await fr.post_result({"id": job.id, "task": {"name": "t", "artifact": ""}}) == 409


async def test_heartbeat_says_cancelled_when_task_changed(api, h, pool):
    job, task = await make_remote_job(h, pool)
    _, fr = await runner_with(api)
    await fr.lease()
    await h.tasks.update(task.id, status="Rejected", rejection_reason="user")
    assert await fr.heartbeat({"id": job.id}) == "cancelled"


# -- result ------------------------------------------------------------------------------------

async def leased(api, h, pool, **kw):
    job, task = await make_remote_job(h, pool, **kw)
    _, fr = await runner_with(api)
    return job, task, fr, await fr.lease()


async def test_result_success_stages_files(api, h, pool, settings):
    job, task, fr, got = await leased(api, h, pool)
    body = {"outcome": "submitted", "summary": "done", "files": [
        {"path": "guides/basil.md", "content_b64": b64("# Basil")}], "deleted": ["old.md"],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2}, "cost_usd": None,
        "duration_ms": 12, "exit_code": 0, "transcript_tail": "tail"}
    r = await api.post(f"/runner/jobs/{job.id}/result", headers=fr.headers, json=body)
    assert r.status_code == 200 and r.json() == {"status": "completed"}
    row = await RemoteJobRepo(pool).get(job.id)
    assert row.status == "completed" and row.finished_at
    assert row.result["files"] == ["guides/basil.md"] and row.result["deleted"] == ["old.md"]
    assert row.result["usage"]["total_tokens"] == 3 and "content_b64" not in json.dumps(row.result)
    staged = Path(settings.WORKSPACE_ROOT) / task.swarm_id / ".daash" / "staging" / job.id
    assert (staged / "guides" / "basil.md").read_text() == "# Basil"
    # a second post and a heartbeat after completion
    assert (await api.post(f"/runner/jobs/{job.id}/result", headers=fr.headers,
                           json=body)).status_code == 409
    assert await fr.heartbeat({"id": job.id}) == "cancelled"


@pytest.mark.parametrize("files,deleted", [
    ([{"path": "../evil.md", "content_b64": ""}], []),
    ([{"path": "/abs.md", "content_b64": ""}], []),
    ([{"path": ".daash/x", "content_b64": ""}], []),
    ([{"path": ".git/config", "content_b64": ""}], []),
    ([{"path": "a\x00b", "content_b64": ""}], []),
    ([{"path": "ok.md", "content_b64": "***"}], []),
    ([], ["../../etc/passwd"]),
])
async def test_result_validation_400_and_job_stays_leased(api, h, pool, settings, files, deleted):
    job, task, fr, _ = await leased(api, h, pool)
    r = await api.post(f"/runner/jobs/{job.id}/result", headers=fr.headers,
                       json={"outcome": "submitted", "files": files, "deleted": deleted})
    assert r.status_code == 400 and r.json()["detail"]
    assert (await RemoteJobRepo(pool).get(job.id)).status == "leased"
    assert not (Path(settings.WORKSPACE_ROOT) / task.swarm_id / ".daash").exists()
    assert not (Path(settings.WORKSPACE_ROOT).parent / "evil.md").exists()
    # the runner can still report the problem as an error
    assert await fr.post_result({"id": job.id, "task": {"name": "t", "artifact": ""}},
                                "error") == 200


async def test_result_other_bad_inputs(api, h, pool):
    job, task, fr, _ = await leased(api, h, pool)
    url = f"/runner/jobs/{job.id}/result"
    assert (await api.post(url, headers=fr.headers, json={"outcome": "weird"})).status_code == 400
    assert (await api.post(url, headers=fr.headers, content=b"not json")).status_code == 400
    assert (await api.post(url, headers=fr.headers, json=[1])).status_code == 400
    assert (await api.post("/runner/jobs/nope/result", headers=fr.headers,
                           json={"outcome": "error"})).status_code == 404


async def test_result_limits_from_job(api, h, pool):
    job, task, fr, _ = await leased(api, h, pool)
    await pool.execute("UPDATE remote_jobs SET payload = jsonb_set(payload, '{limits,max_files}', '1')")
    files = [{"path": f"f{i}.md", "content_b64": b64("x")} for i in range(2)]
    r = await api.post(f"/runner/jobs/{job.id}/result", headers=fr.headers,
                       json={"outcome": "submitted", "files": files})
    assert r.status_code == 400 and "too many" in r.json()["detail"]


async def test_result_body_too_large_413(api, h, pool):
    job, task, fr, _ = await leased(api, h, pool)
    big = b'{"outcome":"submitted","summary":"' + b"x" * (remote.MAX_BODY_BYTES + 10) + b'"}'
    r = await api.post(f"/runner/jobs/{job.id}/result", headers=fr.headers, content=big)
    assert r.status_code == 413


async def test_result_409_for_other_runner_and_unleased(api, h, pool):
    job, task, fr, _ = await leased(api, h, pool)
    _, other = await runner_with(api, "other")
    body = {"outcome": "error", "error": "x"}
    url = f"/runner/jobs/{job.id}/result"
    assert (await api.post(url, headers=other.headers, json=body)).status_code == 409
    await RemoteJobRepo(pool).cancel(job.id)
    assert (await api.post(url, headers=fr.headers, json=body)).status_code == 409


# -- listing ------------------------------------------------------------------------------------

async def test_remote_jobs_listing(api, h, pool):
    job, task = await make_remote_job(h, pool, task_name="Listed")
    await pool.execute("UPDATE remote_jobs SET payload = jsonb_set(payload, '{prompt}', $1::jsonb)",
                       json.dumps("p" * 5000))
    runner, fr = await runner_with(api, "lister")
    await fr.lease()
    jobs = (await api.get("/remote-jobs", params={"swarm_id": task.swarm_id})).json()["jobs"]
    assert len(jobs) == 1
    j = jobs[0]
    assert j["id"] == job.id and j["status"] == "leased" and j["runner_id"] == runner["id"]
    assert j["runner_name"] == "lister" and j["task_name"] == "Listed"
    assert len(j["payload"]["prompt"]) < 2100
    assert (await api.get("/remote-jobs", params={"swarm_id": "other"})).json() == {"jobs": []}
    assert len((await api.get("/remote-jobs")).json()["jobs"]) == 1
    queued, _ = await make_remote_job(h, pool, task_name="Q", artifact="q.md",
                                      team=(await h.team_list())[0])
    names = {x["id"]: x["runner_name"] for x in (await api.get("/remote-jobs")).json()["jobs"]}
    assert names[queued.id] is None
