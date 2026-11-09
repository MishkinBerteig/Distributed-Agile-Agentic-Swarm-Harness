"""End-to-end runner scenarios (docs/DESIGN-0.3.md section 9) against the real containers.

Compose services: `runner` (fake harness, its fake CLI takes ~4 s per job, always running) and
`runner-hang` (same runner identity, its fake CLI hangs; created but stopped until a scenario
starts it). The coordinator's lease is 5 s in this project, so a dead runner's job is re-queued
within seconds. The only non-HTTP channel is the Docker Engine API.
"""
from __future__ import annotations

import os

import httpx
import pytest

from tests.e2e.helpers import COORD_URL, hold, poll

TOKEN = os.environ.get("E2E_RUNNER_TOKEN", "daash_rt_e2e-fixed-test-token")
REMOTE_CFG = {"allowed_harnesses": ["daash", "fake"],
              "delivery_harnesses": [{"harness": "fake", "model": ""}],
              "delivery_agents_per_team": 1}


def jobs(api, sid):
    return api.get("/remote-jobs", params={"swarm_id": sid})["jobs"]


@pytest.fixture(autouse=True)
def runner_services(api, docker):
    """`runner` up and online, `runner-hang` stopped, before and after every test here."""
    def restore():
        if docker.running("runner-hang"):
            docker.kill("runner-hang")
        if not docker.running("runner"):
            docker.start("runner")
    restore()
    poll(lambda: any(r["online"] and r["harnesses"] for r in api.get("/runners")["runners"]),
         timeout=60, desc="the runner to say hello")
    yield
    restore()


def test_mixed_native_and_remote_swarm_reaches_verification(api, fake):
    fake.configure(tasks_per_plan=4)
    cfg = {"allowed_harnesses": ["daash", "fake"], "delivery_agents_per_team": 2,
           "delivery_harnesses": [{"harness": "daash", "model": ""},
                                  {"harness": "fake", "model": ""}]}
    sid = api.create_swarm(config=cfg)["id"]
    api.transition(sid, "start")
    api.wait_verification(sid, timeout=240)

    tasks = api.tasks(sid)
    assert {t["status"] for t in tasks} == {"Done"}
    remote = [t for t in tasks if t["deliverable"].get("harness") == "fake"]
    assert remote, [t["deliverable"] for t in tasks]
    done = [j for j in jobs(api, sid) if j["status"] == "completed"]
    assert done and all(j["runner_name"] == "local-runner" and j["harness"] == "fake" for j in done)
    assert {j["task_id"] for j in done} >= {t["id"] for t in remote}


def test_runner_killed_mid_job_lease_expires_and_another_runner_finishes(api, fake, docker):
    docker.stop("runner")
    docker.start("runner-hang")
    fake.configure(tasks_per_plan=1)
    sid = api.create_swarm(config=REMOTE_CFG)["id"]
    api.transition(sid, "start")
    leased = poll(lambda: [j for j in jobs(api, sid) if j["status"] == "leased"],
                  timeout=120, desc="the hanging runner to lease a job")
    first = leased[0]["id"]

    docker.kill("runner-hang")
    docker.start("runner")
    api.wait_verification(sid, timeout=240)

    tasks = api.tasks(sid)
    assert {t["status"] for t in tasks} == {"Done"}
    all_jobs = jobs(api, sid)
    completed = [j for j in all_jobs if j["status"] == "completed"]
    assert len(completed) == len(tasks) == len({j["task_id"] for j in completed})   # no duplicates
    reclaimed = next(j for j in all_jobs if j["id"] == first)
    assert reclaimed["status"] == "completed" and reclaimed["expiries"] >= 1
    assert not [e for e in api.events(sid) if e["kind"] == "error"]


def test_runner_auth_and_open_admin_api(api):
    with httpx.Client(base_url=COORD_URL, timeout=15) as c:
        bad = c.post("/runner/hello", json={}, headers={"Authorization": "Bearer daash_rt_wrong"})
        assert bad.status_code == 401
        assert c.post("/runner/hello", json={}).status_code == 401           # no token at all
        good = c.post("/runner/hello", json={"host": "e2e", "harnesses": [{"harness": "fake"}]},
                      headers={"Authorization": f"Bearer {TOKEN}"})
        assert good.status_code == 200 and good.json()["lease_seconds"] == 5
    assert api.get("/health")["auth_required"] is False        # DAASH_API_TOKEN unset: admin API open
    assert api.get("/runners")["runners"]


def test_pause_cancels_the_remote_job_and_resume_completes(api, fake):
    fake.configure(tasks_per_plan=1)
    sid = api.create_swarm(config=REMOTE_CFG)["id"]
    api.transition(sid, "start")
    poll(lambda: [j for j in jobs(api, sid) if j["status"] == "leased"],
         timeout=120, desc="a remote job to be running")

    api.transition(sid, "pause")
    poll(lambda: [j for j in jobs(api, sid) if j["status"] == "cancelled"],
         timeout=30, desc="the remote job to be cancelled")
    # the runner learns via its heartbeat and drops the job
    poll(lambda: all(r["active_jobs"] == 0 for r in api.get("/runners")["runners"]),
         timeout=30, desc="the runner to stop the job")
    settled = fake.wait_until_stable(quiet_seconds=2.5)
    hold(lambda: fake.count() == settled
         and not [j for j in jobs(api, sid) if j["status"] in ("leased", "completed")],
         seconds=8, desc="no LLM calls and no remote work while PAUSED")
    assert api.state(sid) == "PAUSED"

    api.transition(sid, "resume")
    api.wait_verification(sid, timeout=240)
    assert {t["status"] for t in api.tasks(sid)} == {"Done"}
    assert [j for j in jobs(api, sid) if j["status"] == "completed"]
