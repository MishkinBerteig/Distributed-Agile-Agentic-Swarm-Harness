"""End-to-end scenarios (docs/DESIGN-0.2.md A.3) against the real containers: coordinator,
engine (0.5 s tick), PostgreSQL, Redis, with the scripted fake LLM behind the gateway URL.

Everything is driven over HTTP; the only other channel is the Docker Engine API (to restart the
engine container). Every wait is a `poll` on a condition with a timeout.
"""
from __future__ import annotations

from tests.e2e.helpers import VISION, hold, poll


def names(tasks):
    return sorted(t["name"] for t in tasks)


def test_happy_path_to_verification_with_metrics_and_artifacts(api, fake):
    fake.configure(tasks_per_plan=2)
    swarm = api.create_swarm()
    assert swarm["lifecycle_state"] == "CREATED" and swarm["teams"] == []
    api.transition(swarm["id"], "start")
    final = api.wait_verification(swarm["id"])
    sid = swarm["id"]

    assert final["definition_of_done"]
    (team,) = final["teams"]
    assert team["name"] == "Team A" and team["status"] == "accomplished"
    assert team["definition_of_done"] and team["last_assessed_at"]
    tasks = api.tasks(sid)
    assert names(tasks) == ["Team A task 1", "Team A task 2"]
    assert {t["status"] for t in tasks} == {"Done"} and {t["attempts"] for t in tasks} == {1}
    assert all(c["status"] == "green" for t in tasks for c in t["criteria"])

    # metrics: tokens counted from the fake's usage, per model and per role
    m = api.get(f"/swarms/{sid}/metrics")
    n_ok = len([c for c in fake.calls() if c["status"] == 200])
    assert m["tokens"]["total"] == 150 * n_ok > 0
    assert set(m["tokens"]["by_model"]) >= {"amazing", "great", "fast"}
    assert m["tasks_by_status"].get("Done") == 2
    assert m["limits"]["tokens_used"] == m["tokens"]["total"]
    assert m["cycle_times"] and all(c["seconds"] >= 0 for c in m["cycle_times"])
    assert not [e for e in api.events(sid) if e["kind"] == "error"]

    # artifacts: the files the agents wrote, readable through the API
    files = api.get(f"/swarms/{sid}/artifacts")["files"]
    assert sorted(f["path"] for f in files) == ["team-a-part-1.md", "team-a-part-2.md"]
    art = api.get(f"/swarms/{sid}/artifacts/team-a-part-1.md")
    assert "Overview" in art["content"] and art["truncated"] is False

    # board, agents and calls
    board = api.get(f"/swarms/{sid}/board")
    assert board["coordinator"]["state"] == "idle"
    agents = board["teams"][0]["agents"]
    assert all(a["state"] == "idle" and a["calls"] > 0 for a in agents)
    delivery = next(a for a in agents if a["role"] == "mission_delivery")
    calls = api.get(f"/agents/{delivery['id']}/calls")["calls"]
    assert calls and calls[0]["transcript"]

    # the decision bus saw the swarm's progress
    kinds = {s["kind"] for s in api.get("/decisions", params={"limit": 200})["signals"]}
    assert {"task.done", "swarm.verification"} <= kinds

    # the jobs ran in a sensible order
    jobs = fake.jobs()
    assert jobs[0] == "bootstrap" and jobs[1] == "standards" and jobs[2] == "plan"
    assert jobs.count("review_quality") == 2 and jobs.count("assess") == 1


def test_rework_then_done(api, fake):
    fake.configure(tasks_per_plan=1, review_fail_attempts=1)
    swarm = api.create_swarm()
    api.transition(swarm["id"], "start")
    api.wait_verification(swarm["id"])
    (task,) = api.tasks(swarm["id"])
    assert task["status"] == "Done" and task["attempts"] == 2
    detail = api.get(f"/tasks/{task['id']}")
    quality = [v for v in detail["verdicts"] if v["judge_role"] == "quality_judgement"]
    assert [(v["attempt"], v["passed"]) for v in quality] == [(1, False), (2, True)]
    assert any(e["kind"] == "rework" and "concluding paragraph" in e["detail"]
               for e in detail["events"])
    works = [c for c in fake.calls() if c["job"] == "work" and c["task_id"] == task["id"]]
    assert works[0]["feedback_in_prompt"] is False and works[-1]["feedback_in_prompt"] is True


def test_gateway_outage_cools_down_then_recovers(api, fake):
    fake.configure(outage_seconds=8, tasks_per_plan=1)           # window opens now
    swarm = api.create_swarm()
    api.transition(swarm["id"], "start")
    sid = swarm["id"]

    errors = poll(lambda: [e for e in api.events(sid) if e["kind"] == "error"], timeout=40,
                  desc="an error event about the unreachable gateway")
    assert "unreachable or overloaded" in errors[0]["detail"]
    assert "retried automatically in 30 seconds" in errors[0]["detail"]
    assert api.state(sid) == "ACTIVE"
    assert any(c["status"] == 503 for c in fake.calls())

    api.wait_verification(sid, timeout=120)                      # after the 30 s cool-down
    (task,) = api.tasks(sid)
    assert task["status"] == "Done" and task["attempts"] == 1    # the outage cost no attempt
    assert fake.jobs().count("bootstrap") == 1
    assert len([e for e in api.events(sid) if e["kind"] == "error"]) == len(errors)


def test_engine_container_restart_mid_run_loses_and_duplicates_nothing(api, fake, docker):
    fake.configure(tasks_per_plan=2, delay_ms=600)
    swarm = api.create_swarm()
    sid = swarm["id"]
    api.transition(sid, "start")
    poll(lambda: any(c["job"] == "work" for c in fake.calls()), timeout=90,
         desc="the first work call to reach the fake LLM")
    before = docker.started_at("engine")

    docker.restart("engine")
    poll(lambda: docker.running("engine") and docker.started_at("engine") != before,
         timeout=60, desc="the engine container to come back")

    api.wait_verification(sid, timeout=180)
    tasks = api.tasks(sid)
    assert names(tasks) == ["Team A task 1", "Team A task 2"]    # none lost, none duplicated
    assert {t["status"] for t in tasks} == {"Done"}
    assert fake.jobs().count("bootstrap") == 1 and fake.jobs().count("plan") == 1
    board = api.get(f"/swarms/{sid}/board")
    assert all(a["state"] == "idle" for a in board["teams"][0]["agents"])   # no stuck BUSY agent
    for t in tasks:
        verdicts = api.get(f"/tasks/{t['id']}")["verdicts"]
        assert sorted({v["judge_role"] for v in verdicts}) == [
            "mission_judgement", "quality_judgement"]


def test_pause_stops_llm_spending_then_resume_completes(api, fake):
    fake.configure(tasks_per_plan=2, delay_ms=300)
    swarm = api.create_swarm()
    sid = swarm["id"]
    api.transition(sid, "start")
    poll(lambda: fake.count() >= 4, timeout=60, desc="the swarm to be working")

    assert api.transition(sid, "pause")["lifecycle_state"] == "PAUSED"
    settled = fake.wait_until_stable(quiet_seconds=2.5)          # in-flight work may still land
    hold(lambda: fake.count() == settled, seconds=5, desc="no LLM calls while PAUSED")
    assert api.state(sid) == "PAUSED"
    assert api.get(f"/swarms/{sid}/board")["coordinator"]["state"] == "idle"

    api.transition(sid, "resume")
    api.wait_verification(sid, timeout=150)
    assert fake.count() > settled
    assert {t["status"] for t in api.tasks(sid)} == {"Done"}


def test_token_limit_pauses_the_swarm_until_the_limit_is_raised(api, fake):
    fake.configure(tasks_per_plan=2)
    swarm = api.create_swarm(config={"limits": {"max_total_tokens": 1000}})
    sid = swarm["id"]
    api.transition(sid, "start")
    poll(lambda: api.state(sid) == "PAUSED", timeout=60, desc="the token limit to pause the swarm")
    paused = api.swarm(sid)
    assert "Token limit reached" in paused["paused_reason"]
    assert any("paused automatically" in e["detail"] for e in api.events(sid))
    spent = fake.wait_until_stable(quiet_seconds=2.5)
    hold(lambda: fake.count() == spent, seconds=3, desc="no LLM calls while PAUSED")
    limits = api.get(f"/swarms/{sid}/metrics")["limits"]
    assert limits["tokens_used"] >= 1000 and limits["max_total_tokens"] == 1000

    cfg = paused["config"]
    cfg["limits"]["max_total_tokens"] = 5_000_000
    assert api.patch(f"/swarms/{sid}", {"config": cfg})["config"]["limits"][
        "max_total_tokens"] == 5_000_000
    api.transition(sid, "resume")
    assert api.swarm(sid)["paused_reason"] is None
    api.wait_verification(sid, timeout=150)
    assert {t["status"] for t in api.tasks(sid)} == {"Done"}


def test_user_rejects_a_task_mid_run_and_the_swarm_carries_on(api, fake):
    fake.configure(tasks_per_plan=3, delay_ms=400)
    swarm = api.create_swarm()
    sid = swarm["id"]
    api.transition(sid, "start")
    victim = poll(lambda: next(iter(api.tasks(sid, status="In-Progress")), None), timeout=90,
                  desc="a task to be In-Progress")
    updated = api.patch(f"/tasks/{victim['id']}", {"status": "Rejected",
                                                    "rejection_reason": "Not needed after all"})
    assert updated["status"] == "Rejected"

    api.wait_verification(sid, timeout=150)
    by_name = {t["name"]: t for t in api.tasks(sid)}
    assert by_name[victim["name"]]["status"] == "Rejected"
    assert by_name[victim["name"]]["rejection_reason"] == "Not needed after all"
    assert sorted(t["status"] for t in by_name.values()) == ["Done", "Done", "Rejected"]
    assert any(e["actor"] == "user" and e["kind"] == "rejected" for e in api.events(sid))
    (team,) = api.swarm(sid)["teams"]
    assert team["status"] == "accomplished"


def test_learn_then_retrospective_then_archive_and_the_learnings_reach_the_next_swarm(api, fake):
    fake.configure(tasks_per_plan=1)
    first = api.create_swarm()
    sid = first["id"]
    api.transition(sid, "start")
    api.wait_verification(sid)

    assert api.transition(sid, "learn")["lifecycle_state"] == "LEARNING"
    poll(lambda: api.swarm(sid)["retrospective_done"], timeout=60, desc="the retrospective")
    learnings = api.get(f"/swarms/{sid}/learnings")["learnings"]
    global_ones = [x for x in learnings if x["swarm_id"] is None]
    assert {x["kind"] for x in global_ones} == {"pattern", "routing"}
    assert fake.jobs().count("retrospective") == 1
    assert api.state(sid) == "LEARNING"                           # waits for the user to archive

    assert api.transition(sid, "archive")["lifecycle_state"] == "ARCHIVED"
    assert api.get("/swarms/active") is None

    # the global learnings are injected into the next swarm's prompts
    fake.configure(record_prompts=True)
    mark = fake.count()
    second = api.create_swarm(vision="Write a short guide to bread.", name="second")
    api.transition(second["id"], "start")
    first_call = poll(lambda: next(iter(fake.calls(since=mark)), None), timeout=30,
                      desc="the second swarm's first call")
    assert "# Learnings" in first_call["system"]
    assert "Give every task a file_exists check." in first_call["system"]
    assert VISION not in first_call["system"]
