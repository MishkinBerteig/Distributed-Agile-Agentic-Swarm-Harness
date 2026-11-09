"""Every HTTP endpoint and status code of the coordinator API (docs/DESIGN-0.2.md A.2).

The real FastAPI app over `httpx.ASGITransport`, a real database and Redis, and the fake LLM
behind `/teams/{id}/quality` and `/models`. Behaviour that needs the engine lives elsewhere.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import TaskCreate
from app.repo import (AgentRepo, EventRepo, LearningRepo, ProposalRepo, SwarmRepo, TaskRepo,
                      VerdictRepo)
from app.services import workspace_dir

V = "Write a short guide to compost."
TEAM = {"name": "Docs", "mission_statement": "To advance the Vision, write the docs."}


async def make_swarm(api, teams=(TEAM,), **extra):
    r = await api.post("/swarms", json={"name": "S", "vision_statement": V,
                                        "teams": list(teams), **extra})
    assert r.status_code == 201, r.text
    return r.json()


async def go(api, swarm_id, action, expect=200):
    r = await api.post(f"/swarms/{swarm_id}/transitions/{action}")
    assert r.status_code == expect, r.text
    return r.json()


def task_body(team_id, name="T1", criteria=None, **kw):
    return {"team_id": team_id, "name": name, "artifact": "a.md",
            "acceptance_criteria": criteria if criteria is not None else [
                {"text": "a.md exists", "check_kind": "file_exists", "check_path": "a.md"},
                {"text": "reads well"}], **kw}


# -- health / config / models -----------------------------------------------------------------

async def test_health(api):
    r = await api.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "auth_required": False}


async def test_config_get_put_and_validation(api):
    cfg = (await api.get("/config")).json()
    assert cfg["values_principles"] == "" and cfg["default_swarm_config"]["limits"]["max_teams"] == 4
    cfg["values_principles"] = "Be kind."
    cfg["default_swarm_config"]["limits"]["max_teams"] = 6
    r = await api.put("/config", json=cfg)
    assert r.status_code == 200 and r.json()["values_principles"] == "Be kind."
    assert (await api.get("/config")).json()["default_swarm_config"]["limits"]["max_teams"] == 6
    bad = {"values_principles": "", "default_swarm_config": {
        "role_models": {"mission_judgement": "not-a-model"}}}
    r = await api.put("/config", json=bad)
    assert r.status_code == 400 and "not-a-model" in r.json()["detail"]
    assert (await api.put("/config", json={"default_swarm_config": {"limits": {
        "max_teams": 0}}})).status_code == 422


async def test_models_reachable_and_unreachable(api, fake):
    body = (await api.get("/models")).json()
    assert body["reachable"] is True and body["models"] == ["instant", "fast", "great", "amazing"]
    fake.configure({"outage_seconds": 60})
    r = await api.get("/models")
    assert r.status_code == 200 and r.json()["reachable"] is False and r.json()["models"] == []


# -- swarms ---------------------------------------------------------------------------------------

async def test_create_swarm_with_and_without_teams(api, settings):
    created = await make_swarm(api, teams=[TEAM, {"name": "Art", "mission_statement": "m"}])
    assert created["lifecycle_state"] == "CREATED" and created["definition_of_done"] is None
    assert [t["name"] for t in created["teams"]] == ["Docs", "Art"]
    roles = sorted(a["role"] for a in created["agents"])
    assert roles.count("mission_delivery") == 2 and roles.count("swarm_coordinator") == 1
    assert roles.count("mission_judgement") == 2 and roles.count("quality_judgement") == 2
    assert workspace_dir(settings, created["id"]).is_dir()
    events = (await api.get(f"/swarms/{created['id']}/events")).json()["events"]
    assert any("created" in e["detail"] for e in events)


async def test_create_swarm_errors(api):
    assert (await api.post("/swarms", json={"vision_statement": V})).status_code == 422
    assert (await api.post("/swarms", json={"name": "", "vision_statement": V})).status_code == 422
    r = await api.post("/swarms", json={"name": "S", "vision_statement": V,
                                        "config": {"allowed_models": []}})
    assert r.status_code == 400
    first = await make_swarm(api, teams=[])
    r = await api.post("/swarms", json={"name": "S2", "vision_statement": V})
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    await go(api, first["id"], "delete")
    assert (await api.post("/swarms", json={"name": "S2", "vision_statement": V})).status_code == 201


async def test_create_swarm_uses_the_global_default_config(api):
    cfg = (await api.get("/config")).json()
    cfg["default_swarm_config"]["limits"]["max_tasks_per_team"] = 3
    await api.put("/config", json=cfg)
    created = await make_swarm(api, teams=[])
    assert created["config"]["limits"]["max_tasks_per_team"] == 3


async def test_list_get_and_active_swarm(api):
    assert (await api.get("/swarms")).json() == {"swarms": []}
    assert (await api.get("/swarms/active")).json() is None
    created = await make_swarm(api)
    assert [s["id"] for s in (await api.get("/swarms")).json()["swarms"]] == [created["id"]]
    assert (await api.get("/swarms/active")).json()["id"] == created["id"]
    got = (await api.get(f"/swarms/{created['id']}")).json()
    assert got["teams"][0]["name"] == "Docs" and len(got["agents"]) == 4
    assert (await api.get("/swarms/nope")).status_code == 404


async def test_patch_swarm(api):
    s = await make_swarm(api)
    r = await api.patch(f"/swarms/{s['id']}", json={"name": "Renamed", "vision_statement": "New"})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    assert r.json()["vision_statement"] == "New"
    r = await api.patch(f"/swarms/{s['id']}", json={"definition_of_done": "- done"})
    assert r.json()["definition_of_done"] == "- done"
    assert (await api.patch(f"/swarms/{s['id']}", json={})).status_code == 200
    cfg = s["config"]
    cfg["limits"]["max_teams"] = 9
    assert (await api.patch(f"/swarms/{s['id']}", json={"config": cfg})).json()[
        "config"]["limits"]["max_teams"] == 9
    cfg["role_models"]["quality_judgement"] = "nope"
    assert (await api.patch(f"/swarms/{s['id']}", json={"config": cfg})).status_code == 400
    await go(api, s["id"], "start")
    r = await api.patch(f"/swarms/{s['id']}", json={"vision_statement": "Too late"})
    assert r.status_code == 409
    assert (await api.patch(f"/swarms/{s['id']}", json={"name": "Still ok"})).status_code == 200
    assert (await api.patch("/swarms/nope", json={"name": "x"})).status_code == 404


async def test_lifecycle_transitions(api, pool):
    s = await make_swarm(api)
    sid = s["id"]
    assert (await go(api, sid, "resume", expect=400)) == {
        "detail": "Cannot 'resume' a swarm in state CREATED. From CREATED you can: start, delete."}
    assert "Unknown swarm action" in (await go(api, sid, "explode", expect=400))["detail"]
    assert (await api.post("/swarms/nope/transitions/start")).status_code == 404

    started = await go(api, sid, "start")
    assert started["lifecycle_state"] == "ACTIVE" and started["started_at"]
    await go(api, sid, "start", expect=400)
    await SwarmRepo(pool).update(sid, paused_reason="Token limit reached")
    assert (await go(api, sid, "pause"))["lifecycle_state"] == "PAUSED"
    resumed = await go(api, sid, "resume")
    assert resumed["lifecycle_state"] == "ACTIVE" and resumed["paused_reason"] is None
    first_start = resumed["started_at"]
    await go(api, sid, "pause")
    assert (await go(api, sid, "resume"))["started_at"] == first_start      # not reset
    verified = await go(api, sid, "verify")
    assert verified["lifecycle_state"] == "VERIFICATION"
    assert (await go(api, sid, "learn"))["lifecycle_state"] == "LEARNING"
    assert (await go(api, sid, "archive"))["lifecycle_state"] == "ARCHIVED"
    await go(api, sid, "delete", expect=400)
    events = (await api.get(f"/swarms/{sid}/events")).json()["events"]
    assert sum(1 for e in events if e["kind"] == "swarm" and e["actor"] == "user") >= 6


async def test_transition_delete_removes_swarm_and_workspace(api, settings):
    s = await make_swarm(api)
    ws = workspace_dir(settings, s["id"])
    (ws / "x.md").write_text("x")
    assert (await go(api, s["id"], "delete")) == {"deleted": True, "id": s["id"]}
    assert (await api.get(f"/swarms/{s['id']}")).status_code == 404
    assert not ws.exists()


async def test_delete_swarm_rules(api, settings):
    s = await make_swarm(api)
    await go(api, s["id"], "start")
    r = await api.delete(f"/swarms/{s['id']}")
    assert r.status_code == 409 and "ACTIVE" in r.json()["detail"]
    await go(api, s["id"], "verify")
    assert (await api.delete(f"/swarms/{s['id']}")).json() == {"deleted": True, "id": s["id"]}
    assert (await api.delete(f"/swarms/{s['id']}")).status_code == 404
    assert (await api.delete("/swarms/nope")).status_code == 404


async def test_feedback(api):
    s = await make_swarm(api)
    sid = s["id"]
    await go(api, sid, "start")
    r = await api.post(f"/swarms/{sid}/feedback", json={"text": "Use shorter sentences."})
    assert r.status_code == 201 and r.json()["lifecycle_state"] == "ACTIVE"
    await go(api, sid, "pause")
    r = await api.post(f"/swarms/{sid}/feedback", json={"text": "Also add a TL;DR."})
    assert r.status_code == 201 and r.json()["lifecycle_state"] == "USER_FEEDBACK"
    texts = [f["text"] for f in (await api.get(f"/swarms/{sid}/feedback")).json()["feedback"]]
    assert texts == ["Use shorter sentences.", "Also add a TL;DR."]
    assert (await api.get(f"/swarms/{sid}")).json()["lifecycle_state"] == "USER_FEEDBACK"
    await go(api, sid, "resume")
    assert (await api.post(f"/swarms/{sid}/feedback", json={"text": ""})).status_code == 422
    assert (await api.post("/swarms/nope/feedback", json={"text": "x"})).status_code == 404
    assert (await api.get("/swarms/nope/feedback")).status_code == 404


# -- board / metrics / events / artifacts / learnings / proposals / calls ------------------

async def test_board(api):
    s = await make_swarm(api)
    team_id = s["teams"][0]["id"]
    await api.post("/tasks", json=task_body(team_id))
    board = (await api.get(f"/swarms/{s['id']}/board")).json()
    assert board["swarm"]["id"] == s["id"] and board["coordinator"]["role"] == "swarm_coordinator"
    entry = board["teams"][0]
    assert entry["team"]["name"] == "Docs" and len(entry["agents"]) == 3
    assert [t["name"] for t in entry["tasks"]] == ["T1"] and len(entry["tasks"][0]["criteria"]) == 2
    assert (await api.get("/swarms/nope/board")).status_code == 404


async def test_metrics(api, pool):
    s = await make_swarm(api)
    from app.repo import CallRepo
    await CallRepo(pool).add(s["id"], "mission_delivery", "fast", "work_task", total_tokens=900,
                             prompt_tokens=600, completion_tokens=300)
    m = (await api.get(f"/swarms/{s['id']}/metrics")).json()
    assert m["tokens"]["total"] == 900 and m["tokens"]["by_model"] == {"fast": 900}
    assert m["limits"]["tokens_used"] == 900 and m["limits"]["max_total_tokens"] == 5_000_000
    assert m["p90_cycle_seconds"] is None and "tasks_by_status" in m and "error_rate" in m
    assert (await api.get("/swarms/nope/metrics")).status_code == 404


async def test_events_limit_and_filters(api, pool):
    s = await make_swarm(api, teams=[TEAM, {"name": "Art", "mission_statement": "m"}])
    t1, t2 = s["teams"]
    task = (await api.post("/tasks", json=task_body(t1["id"]))).json()
    for i in range(5):
        await EventRepo(pool).add(s["id"], "swarm", f"note {i}", team_id=t2["id"])
    allev = (await api.get(f"/swarms/{s['id']}/events", params={"limit": 1000})).json()["events"]
    assert allev[0]["created_at"] >= allev[-1]["created_at"]                 # newest first
    assert len((await api.get(f"/swarms/{s['id']}/events", params={"limit": 2})).json()["events"]) == 2
    by_team = (await api.get(f"/swarms/{s['id']}/events", params={"team_id": t2["id"]})).json()
    assert len([e for e in by_team["events"] if e["detail"].startswith("note")]) == 5
    by_task = (await api.get(f"/swarms/{s['id']}/events", params={"task_id": task["id"]})).json()
    assert len(by_task["events"]) == 1 and by_task["events"][0]["kind"] == "created"
    assert (await api.get("/swarms/nope/events")).status_code == 404


async def test_artifacts(api, settings):
    s = await make_swarm(api)
    ws = workspace_dir(settings, s["id"])
    assert (await api.get(f"/swarms/{s['id']}/artifacts")).json() == {"files": []}
    (ws / "docs").mkdir()
    (ws / "docs" / "a.md").write_text("# Hello\nworld")
    (ws / "big.txt").write_text("x" * 250_000)
    files = {f["path"]: f for f in (await api.get(f"/swarms/{s['id']}/artifacts")).json()["files"]}
    assert set(files) == {"docs/a.md", "big.txt"} and files["docs/a.md"]["size"] == 13
    r = await api.get(f"/swarms/{s['id']}/artifacts/docs/a.md")
    assert r.status_code == 200 and r.json() == {"path": "docs/a.md", "content": "# Hello\nworld",
                                                 "truncated": False}
    big = (await api.get(f"/swarms/{s['id']}/artifacts/big.txt")).json()
    assert big["truncated"] is True and len(big["content"]) == 200_000
    assert (await api.get(f"/swarms/{s['id']}/artifacts/missing.md")).status_code == 404
    assert (await api.get(f"/swarms/{s['id']}/artifacts/docs")).status_code == 404   # a directory
    for evil in ("%2e%2e%2fsecret.txt", "%2Fetc%2Fpasswd", "docs%2F..%2F..%2Fx"):
        assert (await api.get(f"/swarms/{s['id']}/artifacts/{evil}")).status_code == 404
    assert (await api.get("/swarms/nope/artifacts")).status_code == 404
    assert (await api.get("/swarms/nope/artifacts/a.md")).status_code == 404


async def test_learnings_proposals_and_agent_calls(api, pool):
    s = await make_swarm(api)
    team = s["teams"][0]
    await LearningRepo(pool).add("swarm lesson", "pattern", swarm_id=s["id"])
    await LearningRepo(pool).add("global lesson", "routing", swarm_id=None)
    got = (await api.get(f"/swarms/{s['id']}/learnings")).json()["learnings"]
    assert {x["content"] for x in got} == {"swarm lesson", "global lesson"}
    assert (await api.get("/swarms/nope/learnings")).status_code == 404

    delivery = next(a for a in s["agents"] if a["role"] == "mission_delivery")
    await ProposalRepo(pool).create(s["id"], team["id"], delivery["id"], "Spin", "M", "r", 1)
    props = (await api.get(f"/swarms/{s['id']}/proposals")).json()["proposals"]
    assert [p["name"] for p in props] == ["Spin"] and props[0]["status"] == "pending"
    assert (await api.get("/swarms/nope/proposals")).status_code == 404

    from app.repo import CallRepo
    for i in range(3):
        await CallRepo(pool).add(s["id"], "mission_delivery", "fast", "work_task",
                                 agent_id=delivery["id"], transcript=[{"n": i}])
    calls = (await api.get(f"/agents/{delivery['id']}/calls")).json()["calls"]
    assert len(calls) == 3 and calls[0]["transcript"]
    assert len((await api.get(f"/agents/{delivery['id']}/calls", params={"limit": 2})
                ).json()["calls"]) == 2
    assert (await api.get("/agents/nope/calls")).status_code == 404


# -- teams -----------------------------------------------------------------------------------------

async def test_team_crud(api):
    assert (await api.get("/teams")).json() == {"teams": []}
    s = await make_swarm(api, teams=[])
    r = await api.post("/teams", json={"swarm_id": s["id"], "name": "Docs", "mission_statement": "m"})
    assert r.status_code == 201 and r.json()["creator_kind"] == "user" and len(r.json()["agents"]) == 3
    team_id = r.json()["id"]
    assert [t["id"] for t in (await api.get("/teams")).json()["teams"]] == [team_id]       # live swarm
    assert [t["id"] for t in (await api.get("/teams", params={"swarm_id": s["id"]})).json()[
        "teams"]] == [team_id]
    got = (await api.get(f"/teams/{team_id}")).json()
    assert got["name"] == "Docs" and len(got["agents"]) == 3
    upd = (await api.patch(f"/teams/{team_id}", json={"name": "Docs2", "status": "accomplished"})
           ).json()
    assert (upd["name"], upd["status"]) == ("Docs2", "accomplished")
    assert (await api.patch(f"/teams/{team_id}", json={})).json()["name"] == "Docs2"
    assert (await api.patch(f"/teams/{team_id}", json={"status": "bogus"})).status_code == 422
    assert (await api.post("/teams", json={"swarm_id": "nope", "name": "x",
                                           "mission_statement": "m"})).status_code == 404
    assert (await api.post("/teams", json={"swarm_id": s["id"], "name": "",
                                           "mission_statement": "m"})).status_code == 422
    assert (await api.delete(f"/teams/{team_id}")).json() == {"deleted": True, "id": team_id}
    assert (await api.get(f"/teams/{team_id}")).status_code == 404
    for call in (api.get(f"/teams/{team_id}"), api.patch(f"/teams/{team_id}", json={"name": "x"}),
                 api.delete(f"/teams/{team_id}"), api.get(f"/teams/{team_id}/quality")):
        assert (await call).status_code == 404


async def test_cannot_add_a_team_to_an_archived_swarm(api):
    s = await make_swarm(api, teams=[])
    await go(api, s["id"], "start")
    await go(api, s["id"], "verify")
    await go(api, s["id"], "archive")
    r = await api.post("/teams", json={"swarm_id": s["id"], "name": "Late", "mission_statement": "m"})
    assert r.status_code == 409


async def test_team_quality_generation(api, fake):
    s = await make_swarm(api)
    tid = s["teams"][0]["id"]
    assert (await api.get(f"/teams/{tid}/quality")).json() == {
        "team_id": tid, "definition_of_ready": None, "definition_of_done": None}
    r = await api.post(f"/teams/{tid}/quality", json={"dor_guidance": "Short", "dod_guidance": "Dry"})
    assert r.status_code == 201
    assert r.json()["definition_of_done"].startswith("- Every file is complete")
    assert (await api.get(f"/teams/{tid}/quality")).json()["definition_of_ready"]
    assert fake.calls_for("standards")[-1]["role"] == "Quality Judgement"
    assert (await api.post(f"/teams/{tid}/quality")).status_code == 201           # body optional
    fake.configure({"malformed_next": 2})                          # unusable twice -> 502
    r = await api.post(f"/teams/{tid}/quality", json={})
    assert r.status_code == 502 and "could not generate" in r.json()["detail"]
    fake.configure({"outage_seconds": 60})
    assert (await api.post(f"/teams/{tid}/quality", json={})).status_code == 502
    assert (await api.post("/teams/nope/quality", json={})).status_code == 404


# -- tasks ---------------------------------------------------------------------------------------

async def test_task_create_and_list(api):
    assert (await api.get("/tasks")).json() == {"tasks": []}
    s = await make_swarm(api, teams=[TEAM, {"name": "Art", "mission_statement": "m"}])
    t1, t2 = s["teams"]
    r = await api.post("/tasks", json=task_body(t1["id"]))
    assert r.status_code == 201
    task = r.json()
    assert task["status"] == "Pending" and task["created_by"] == "user" and task["swarm_id"] == s["id"]
    assert [c["ordinal"] for c in task["criteria"]] == [1, 2]
    await api.post("/tasks", json=task_body(t2["id"], "T2"))
    assert len((await api.get("/tasks")).json()["tasks"]) == 2                      # live swarm
    assert [t["name"] for t in (await api.get("/tasks", params={"team_id": t2["id"]})).json()[
        "tasks"]] == ["T2"]
    assert (await api.get("/tasks", params={"swarm_id": s["id"], "status": "Done"})
            ).json()["tasks"] == []
    assert len((await api.get("/tasks", params={"swarm_id": s["id"], "status": "Pending"})
                ).json()["tasks"]) == 2


async def test_task_create_validation(api):
    s = await make_swarm(api)
    tid = s["teams"][0]["id"]
    assert (await api.post("/tasks", json=task_body("nope"))).status_code == 404
    r = await api.post("/tasks", json=task_body(tid, criteria=[]))
    assert r.status_code == 422 and "at least one acceptance criterion" in r.json()["detail"]
    r = await api.post("/tasks", json=task_body(tid, criteria=[
        {"text": "contains", "check_kind": "file_contains", "check_path": "a.md"}]))
    assert r.status_code == 422 and "regular expression" in r.json()["detail"]
    r = await api.post("/tasks", json=task_body(tid, criteria=[
        {"text": "bad path", "check_kind": "file_exists", "check_path": "../x"}]))
    assert r.status_code == 422
    assert (await api.post("/tasks", json={"team_id": tid, "name": ""})).status_code == 422


async def test_task_detail_patch_and_delete(api, pool):
    s = await make_swarm(api)
    tid = s["teams"][0]["id"]
    task = (await api.post("/tasks", json=task_body(tid))).json()
    child = await TaskRepo(pool).create(s["id"], TaskCreate(
        team_id=tid, name="Sub", parent_id=task["id"], acceptance_criteria=[{"text": "x"}]))
    await VerdictRepo(pool).add(task["id"], 1, "quality_judgement", None, True, 1.0, "fine")
    detail = (await api.get(f"/tasks/{task['id']}")).json()
    assert [c["id"] for c in detail["children"]] == [child.id]
    assert detail["verdicts"][0]["reasoning"] == "fine" and detail["events"][0]["kind"] == "created"
    assert (await api.get("/tasks/nope")).status_code == 404

    r = await api.patch(f"/tasks/{task['id']}", json={"status": "Rejected",
                                                      "rejection_reason": "Out of scope"})
    assert r.status_code == 200 and r.json()["status"] == "Rejected"
    assert r.json()["rejection_reason"] == "Out of scope"
    ev = (await api.get(f"/tasks/{task['id']}")).json()["events"]
    assert any(e["kind"] == "rejected" and e["actor"] == "user" and "Out of scope" in e["detail"]
               for e in ev)

    await TaskRepo(pool).update(task["id"], attempts=3, assigned_agent_id=None,
                                committed_at=datetime.now(timezone.utc))
    r = await api.patch(f"/tasks/{task['id']}", json={"status": "Pending"})           # reopen
    reopened = r.json()
    assert reopened["status"] == "Pending" and reopened["attempts"] == 0
    assert reopened["committed_at"] is None and reopened["done_at"] is None
    r = await api.patch(f"/tasks/{task['id']}", json={"name": "Renamed", "priority": 3})
    assert r.json()["name"] == "Renamed" and r.json()["priority"] == 3
    assert (await api.patch(f"/tasks/{task['id']}", json={})).json()["name"] == "Renamed"
    assert (await api.patch(f"/tasks/{task['id']}", json={"status": "Nonsense"})).status_code == 422
    assert (await api.patch("/tasks/nope", json={"name": "x"})).status_code == 404

    assert (await api.delete(f"/tasks/{task['id']}")).status_code == 204
    assert (await api.get(f"/tasks/{task['id']}")).status_code == 404
    assert (await api.delete(f"/tasks/{task['id']}")).status_code == 404
    assert await TaskRepo(pool).get(child.id) is None                                # cascade


# -- decision bus (unchanged endpoints) --------------------------------------------------------

async def test_decision_signals(api):
    r = await api.post("/decisions", json={"kind": "task.done", "team_id": "t1", "payload": {"a": 1}})
    assert r.status_code == 201 and r.json()["kind"] == "task.done"
    assert (await api.post("/decisions", json={"kind": ""})).status_code == 422
    signals = (await api.get("/decisions")).json()["signals"]
    assert [s["kind"] for s in signals] == ["task.done"] and signals[0]["payload"] == {"a": 1}
    assert len((await api.get("/decisions", params={"limit": 1})).json()["signals"]) == 1


async def test_bus_proposals_and_votes(api):
    r = await api.post("/proposals", json={"team_id": "t1", "question": "Ship?", "quorum": 2})
    assert r.status_code == 201 and r.json()["status"] == "pending"
    pid = r.json()["id"]
    assert (await api.get(f"/proposals/{pid}")).json()["question"] == "Ship?"
    assert (await api.get("/proposals/nope")).status_code == 404
    assert [p["id"] for p in (await api.get("/proposals", params={"team_id": "t1"})).json()[
        "proposals"]] == [pid]
    assert (await api.get("/proposals", params={"team_id": "other"})).json()["proposals"] == []
    assert (await api.post("/proposals", json={"team_id": "t1", "question": "", "quorum": 1}
                           )).status_code == 422

    v1 = await api.post(f"/proposals/{pid}/votes", json={"voter": "a", "approve": True})
    assert v1.status_code == 200 and v1.json()["status"] == "pending"
    dup = await api.post(f"/proposals/{pid}/votes", json={"voter": "a", "approve": True})
    assert dup.status_code == 409
    v2 = await api.post(f"/proposals/{pid}/votes", json={"voter": "b", "approve": True})
    assert v2.json()["status"] == "approved"
    late = await api.post(f"/proposals/{pid}/votes", json={"voter": "c", "approve": False})
    assert late.status_code == 409
    missing = await api.post("/proposals/nope/votes", json={"voter": "a", "approve": True})
    assert missing.status_code == 404
    kinds = [s["kind"] for s in (await api.get("/decisions")).json()["signals"]]
    assert "proposal_approved" in kinds and "proposal_vote" in kinds
