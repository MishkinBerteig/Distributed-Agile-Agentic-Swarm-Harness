"""Connected agents against the real database (docs/DESIGN-0.4.md section 5): connect through the
admin API, the owner's approval detected by the engine, connected jobs driven to Review and Done
by an in-process fake A2A agent (through DAASH's MCP tools, and with reply file blocks),
handover, re-sending after a gateway restart, pausing, discovery, availability, max_jobs, config
validation, and the job of an agent on an agent host."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app import mcp_server
from app.models import HarnessSpec
from app.remote import hash_token
from app.repo import (ConnectedAgentRepo, MemoryRepo, MessageRepo, RemoteJobRepo, RunnerRepo)
from tests.fakes.fake_a2a_agent import FakeA2AAgent
from tests.integration.helpers import swarm_config

VISION = "Write a short guide to compost."


@pytest.fixture
def jane(api, monkeypatch, h):
    """A fake OpenClaw gateway 'Jane' whose MCP calls reach DAASH's app in-process; DAASH's
    connector calls (API and engine) reach the fake."""
    from app import main
    agent = FakeA2AAgent("Jane", mcp_transport=httpx.ASGITransport(app=main.app),
                         mcp_url="http://test/mcp")
    async def handler(req: httpx.Request) -> httpx.Response:
        if req.url.port != 18789:           # the gateway listens on its own port only
            raise httpx.ConnectError("connection refused")
        return await agent.httpx_handler(req)

    t = httpx.MockTransport(handler)
    monkeypatch.setattr(main, "connector_transport", t)
    h.engine.jobs.connector_transport = t
    h.engine.jobs.connected_poll_seconds = 0.02
    h.engine.jobs.remote_poll_seconds = 0.02
    return agent


async def connect_and_approve(api, h, agent, **body) -> dict:
    r = await api.post("/connected-agents", json={"address": "http://jane:18789", **body})
    assert r.status_code == 201, r.text
    out = r.json()
    agent.approve(out["approval"]["a2a_token"], mcp_token=out["approval"]["mcp_token"])
    connected = await h.engine.check_connected_pending()
    assert [c.id for c in connected] == [out["agent"]["id"]]
    return out


def connected_config(*rows, delivery=None, **limits):
    cfg = swarm_config(delivery or len(rows), **limits)
    cfg.allowed_harnesses = ["daash", "connected"]
    cfg.delivery_harnesses = list(rows)
    return cfg


async def swarm_with_task(h, cfg, assignee="Delivery-1", name="Intro", artifact="intro.md"):
    await h.create_swarm(VISION, config=cfg, dod="- The guide exists")
    team = await h.seed_team("Team A")
    agents = await h.agent_list(team)
    agent = next(a for a in agents if a.name == assignee)
    task = await h.add_task(team, name, artifact=artifact, status="In-Progress",
                            assigned_agent_id=agent.id, complexity="moderate")
    await h.transition("start")
    return team, agent, task


async def status_of(h, task):
    return (await h.tasks.get(task.id)).status


# -- connect, approval, availability ----------------------------------------------------------------

async def test_connect_pending_until_the_owner_approves(api, h, jane, pool):
    await h.create_swarm(VISION)                       # a live swarm for the feed's event
    r = await api.post("/connected-agents", json={"address": "http://jane:18789"})
    assert r.status_code == 201, r.text
    body = r.json()
    a, step = body["agent"], body["approval"]
    assert (a["name"], a["status"], a["kind"], a["protocol"]) == ("Jane", "pending", "openclaw",
                                                                  "a2a")
    assert a["endpoint"] == "http://jane:18789/a2a/v1"           # entered origin, card's path
    assert "token" not in a and "mcp_token_hash" not in a
    assert step["command"].startswith("openclaw config patch --stdin")
    assert step["mcp_token"].startswith("dca_") and step["warnings"]   # no public URL in tests
    # never again: list and get carry no secret
    listed = (await api.get("/connected-agents")).text
    assert step["a2a_token"] not in listed and step["mcp_token"] not in listed
    assert step["a2a_token"] not in (await api.get(f"/connected-agents/{a['id']}")).text
    # waiting: the check gets 401 from the gateway
    assert await h.engine.check_connected_pending() == []
    got = (await api.get(f"/connected-agents/{a['id']}")).json()
    assert got["status"] == "pending" and got["error_kind"] == "token_rejected"
    # the owner applies the patch: DAASH notices by itself
    jane.approve(step["a2a_token"], mcp_token=step["mcp_token"])
    assert [c.name for c in await h.engine.check_connected_pending()] == ["Jane"]
    got = (await api.get(f"/connected-agents/{a['id']}")).json()
    assert (got["status"], got["availability"], got["last_error"]) == ("connected", "available",
                                                                      None)
    assert any("Jane accepted DAASH's connection" in e.detail for e in await h.event_list())
    # the MCP token from the step works (no active job yet: a polite refusal, not a 401)
    r = await api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"Authorization": f"Bearer {step['mcp_token']}"})
    assert r.status_code == 200
    assert "write_file" in {t["name"] for t in r.json()["result"]["tools"]}


async def test_connect_before_the_a2a_channel_exists(api, h, jane, pool):
    """A gateway whose owner has not turned A2A on serves no card: connecting still works and
    the approval step is what makes it answer; the card is fetched once it does."""
    jane.channel_on = False
    # a bare host name: DAASH tries the default ports and recognises the gateway by its page
    r = await api.post("/connected-agents", json={"address": "jane"})
    assert r.status_code == 201, r.text
    out = r.json()
    a = out["agent"]
    assert (a["name"], a["url"], a["endpoint"], a["card"]) == (
        "OpenClaw (jane)", "http://jane:18789", "http://jane:18789/a2a/v1", None)
    assert await h.engine.check_connected_pending() == []
    got = (await api.get(f"/connected-agents/{a['id']}")).json()
    assert got["error_kind"] == "unreachable"            # 404 at the endpoint before approval
    jane.approve(out["approval"]["a2a_token"])
    assert len(await h.engine.check_connected_pending()) == 1
    got = (await api.get(f"/connected-agents/{a['id']}")).json()
    assert got["status"] == "connected" and got["card"]["skills"][0]["name"] == "Jane"
    # Hermes before its API server is enabled: nothing answers, the step is still given
    r = await api.post("/connected-agents", json={"address": "herm", "protocol": "hermes-api"})
    assert r.status_code == 201, r.text
    h_out = r.json()
    assert h_out["agent"]["url"] == "http://herm:8642" and h_out["agent"]["kind"] == "hermes"
    assert "API_SERVER_KEY=" + h_out["approval"]["api_key"] in h_out["approval"]["env"]


async def test_reset_retry_patch_and_delete(api, h, jane, pool):
    out = await connect_and_approve(api, h, jane)
    aid = out["agent"]["id"]
    r = await api.post(f"/connected-agents/{aid}/reset-tokens")
    assert r.status_code == 200
    new = r.json()
    assert new["agent"]["status"] == "pending"
    assert new["approval"]["a2a_token"] != out["approval"]["a2a_token"]
    old_mcp = await api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             headers={"Authorization": f"Bearer {out['approval']['mcp_token']}"})
    assert old_mcp.status_code == 401
    assert (await api.post(f"/connected-agents/{aid}/retry")).status_code == 200
    r = await api.patch(f"/connected-agents/{aid}", json={
        "name": "Jane D", "capabilities": ["edit_files", "web_research", "bogus"], "max_jobs": 2,
        "enabled": False})
    assert r.status_code == 200
    v = r.json()
    assert (v["name"], v["capabilities"], v["max_jobs"], v["availability"]) == (
        "Jane D", ["edit_files", "web_research"], 2, "disabled")
    assert (await api.patch(f"/connected-agents/{aid}", json={"api_key": "x" * 10})
            ).status_code == 422          # A2A tokens are DAASH's
    assert (await api.delete(f"/connected-agents/{aid}")).status_code == 204
    assert (await api.get(f"/connected-agents/{aid}")).status_code == 404


async def test_availability_lists_connected_agents(api, h, jane, pool):
    out = await connect_and_approve(api, h, jane)
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=out["agent"]["id"]))
    r = await api.put("/config", json={"values_principles": "", "default_swarm_config":
                                       cfg.model_dump(mode="json")})
    assert r.status_code == 200, r.text
    av = (await api.get("/availability")).json()
    row = next(c for c in av["connected_agents"] if c["name"] == "Jane")
    assert (row["protocol"], row["kind"], row["availability"], row["allowed"],
            row["delivery_rows"]) == ("a2a", "openclaw", "available", True, 1)
    assert row["daash_tools"] == {"in_use": False, "last_call": None}
    assert "token" not in row and av["agent_endpoint"]["configured"] is False


async def test_config_must_name_an_existing_enabled_agent(api, h, jane, pool):
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent="nope"))
    r = await api.post("/swarms", json={"name": "S", "vision_statement": VISION,
                                        "config": cfg.model_dump(mode="json")})
    assert r.status_code == 400 and "does not exist" in r.json()["detail"]


# -- connected work ----------------------------------------------------------------------------------

async def test_connected_job_through_daash_tools_reaches_done(api, h, jane, pool):
    out = await connect_and_approve(api, h, jane)
    aid = out["agent"]["id"]
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=aid))
    team, agent, task = await swarm_with_task(h, cfg)
    assert (agent.harness, agent.provider, agent.model) == ("connected", aid, "")
    await h.run_until(lambda: status_of_done(h, task), label="task Done")

    t = await h.tasks.get(task.id)
    d = t.deliverable
    assert (d["harness"], d["connected_agent"], d["protocol"]) == ("connected", "Jane", "a2a")
    assert d["files"] == ["intro.md"] and d["mcp_calls"] >= 6
    assert "Overview" in h.workspace_file("intro.md")
    names = [c[0] for c in jane.tool_calls]
    assert names[:2] == ["current_task", "list_files"]
    assert {"write_file", "read_file", "remember", "send_message"} <= set(names)
    # what the agent was sent: system prompt first, the job id, its own session per attempt
    task_msg = next(iter(jane.tasks.values()))
    assert task_msg.text.startswith("# Vision") and "Your DAASH job id: " in task_msg.text
    assert task_msg.context_id.startswith("daash-") and task_msg.context_id.endswith("-a0")
    # DAASH memory and messages from the connected agent, and its tool calls in the feed
    assert any("Jane wrote intro.md" in m.content for m in await MemoryRepo(pool).list(agent.id))
    msgs = await MessageRepo(pool).list_for_swarm(h.swarm_id)
    assert any(m.from_agent_id == agent.id and m.audience == "coordinator" for m in msgs)
    details = [e.detail for e in await h.event_list()]
    assert any("Delivery-1 (Jane) used DAASH tool write_file" in x for x in details)
    assert any(x.startswith("Delivery-1 (Jane, via A2A) submitted 'Intro'") for x in details)
    ca = await ConnectedAgentRepo(pool).get(aid)
    assert ca.mcp_seen_at is not None and ca.last_error is None
    job = (await RemoteJobRepo(pool).list(h.swarm_id))[0]
    assert (job.harness, job.status, job.runner_id) == ("connected", "completed", None)
    # the job ended: its MCP calls are refused now
    text, err = await mcp_tool(api, out["approval"]["mcp_token"], "remember", content="late",
                               job=job.id)
    assert err and "ended" in text


async def status_of_done(h, task):
    return await status_of(h, task) == "Done"


async def mcp_tool(api, token, name, **arguments):
    r = await api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}},
                       headers={"Authorization": f"Bearer {token}"})
    res = r.json()["result"]
    return res["content"][0]["text"], res["isError"]


async def test_connected_job_with_reply_file_blocks(api, h, jane, pool):
    jane.mode = "blocks"
    out = await connect_and_approve(api, h, jane)
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=out["agent"]["id"]))
    team, agent, task = await swarm_with_task(h, cfg, artifact="guide/intro.md")
    await h.run_until(lambda: status_of_done(h, task), label="task Done")
    t = await h.tasks.get(task.id)
    assert t.deliverable["files"] == ["guide/intro.md"] and t.deliverable["mcp_calls"] == 0
    assert "Overview" in h.workspace_file("guide/intro.md")
    assert "<<<FILE" not in t.deliverable["summary"] and "I wrote" in t.deliverable["summary"]
    assert jane.tool_calls == []


async def test_failed_attempt_hands_the_task_to_another_member(api, h, jane, pool):
    jane.mode = "fail"
    out = await connect_and_approve(api, h, jane)
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=out["agent"]["id"]),
                           HarnessSpec(harness="daash", model=""))
    team, agent, task = await swarm_with_task(h, cfg)
    await h.run_until(lambda: status_of_done(h, task), label="task Done")
    t = await h.tasks.get(task.id)
    d2 = next(a for a in await h.agent_list(team) if a.name == "Delivery-2")
    assert t.assigned_agent_id == d2.id and t.attempts >= 1
    details = [e.detail for e in await h.event_list()]
    assert "Jane could not finish 'Intro'; handing it to Delivery-2." in details
    assert any("Delivery-1 (Jane, via A2A) did not finish" in x for x in details)


async def test_a_forgotten_task_is_sent_again_without_an_attempt(api, h, jane, pool, clock):
    jane.mode = "forget"
    out = await connect_and_approve(api, h, jane)
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=out["agent"]["id"]))
    team, agent, task = await swarm_with_task(h, cfg)

    async def advanced():
        clock.advance(400)       # past the cooldown the infrastructure error caused
        return await status_of(h, task) == "Done"
    await h.run_until(advanced, label="task Done")
    jobs = await RemoteJobRepo(pool).list(h.swarm_id)
    assert sorted(j.status for j in jobs) == ["abandoned", "completed"]
    t = await h.tasks.get(task.id)
    assert t.attempts == 1        # only the review counted
    assert any("no longer knows 'Intro'" in e.detail for e in await h.event_list())


async def test_pause_cancels_the_job_and_its_tools(api, h, jane, pool):
    jane.work_delay = 0.4
    out = await connect_and_approve(api, h, jane)
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=out["agent"]["id"]))
    team, agent, task = await swarm_with_task(h, cfg)
    repo = RemoteJobRepo(pool)

    async def sent():
        jobs = await repo.list(h.swarm_id)
        return bool(jobs) and bool(jobs[0].payload.get("external_id"))
    await h.engine.tick()
    await h.wait_for(sent, label="the message sent")
    await h.transition("pause")
    await h.engine.tick()                       # cancels the running work job
    job = (await repo.list(h.swarm_id))[0]
    assert job.status == "cancelled"
    for bg in jane.background:
        await asyncio.gather(bg, return_exceptions=True)
    # its first tool call was refused: the job ended
    assert [c[0] for c in jane.tool_calls] == ["current_task"]
    assert "has ended" in jane.tool_calls[0][2]
    assert (await h.tasks.get(task.id)).status == "In-Progress"


async def test_unreachable_agent_falls_back_to_the_built_in_agent(api, h, pool, monkeypatch):
    def down(req):
        raise httpx.ConnectError("no route")
    h.engine.jobs.connector_transport = httpx.MockTransport(down)
    ca = await ConnectedAgentRepo(pool).create(
        "Jane", "a2a", "http://jane:18789", endpoint="http://jane:18789/a2a/v1", token="t",
        kind="openclaw")
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=ca.id))
    cfg.remote_fallback_minutes = 1
    team, agent, task = await swarm_with_task(h, cfg)
    await h.step()                               # unreachable: RemoteError, cooldown
    job = (await RemoteJobRepo(pool).list(h.swarm_id))[0]
    assert job.status == "queued" and job.payload.get("error_since")
    assert (await ConnectedAgentRepo(pool).get(ca.id)).last_error.startswith("[unreachable]")
    await pool.execute("UPDATE remote_jobs SET payload = jsonb_set(payload, '{error_since}', "
                       "to_jsonb('2020-01-01T00:00:00+00:00'::text)) WHERE id = $1", job.id)
    h.clock.advance(400)
    await h.run_until(lambda: status_of_done(h, task), label="task Done")
    assert (await RemoteJobRepo(pool).get(job.id)).status == "abandoned"
    assert any("on the built-in agent instead" in e.detail for e in await h.event_list())


async def test_max_jobs_caps_rows_of_one_agent(api, h, jane, pool):
    out = await connect_and_approve(api, h, jane, max_jobs=1)
    aid = out["agent"]["id"]
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=aid),
                           HarnessSpec(harness="connected", connected_agent=aid))
    await h.create_swarm(VISION, config=cfg, dod="- The guide exists")
    team = await h.seed_team("Team A")
    for n in ("One", "Two"):
        await h.add_task(team, n, status="Ready", priority=1 if n == "One" else 2)
    await h.transition("start")
    await h.engine.tick()                        # commits inline, then launches work
    statuses = sorted(t.status for t in await h.task_list())
    assert statuses == ["In-Progress", "Ready"]
    await h.close()


async def test_hermes_api_job_reaches_done(api, h, pool, monkeypatch):
    from app import main
    from tests.fakes.fake_a2a_agent import FakeHermesAPI
    herm = FakeHermesAPI(mcp_transport=httpx.ASGITransport(app=main.app),
                         mcp_url="http://test/mcp")
    t = httpx.MockTransport(herm.httpx_handler)
    monkeypatch.setattr(main, "connector_transport", t)
    h.engine.jobs.connector_transport = t
    h.engine.jobs.connected_poll_seconds = 0.02
    r = await api.post("/connected-agents", json={"address": "herm", "protocol": "hermes-api",
                                                  "name": "Hermes"})
    assert r.status_code == 201, r.text
    out = r.json()
    key = out["approval"]["api_key"]
    herm.approve(key, mcp_token=out["approval"]["mcp_token"])     # the owner's .env + restart
    assert [c.name for c in await h.engine.check_connected_pending()] == ["Hermes"]
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=out["agent"]["id"]))
    team, agent, task = await swarm_with_task(h, cfg)
    await h.run_until(lambda: status_of_done(h, task), label="task Done")
    d = (await h.tasks.get(task.id)).deliverable
    assert (d["connected_agent"], d["protocol"], d["files"]) == ("Hermes", "hermes-api",
                                                                 ["intro.md"])
    assert herm.sessions and herm.sessions[0].endswith("-a0")
    assert {"write_file", "remember", "send_message"} <= {c[0] for c in herm.tool_calls}
    assert any("(Hermes, via the Hermes API) submitted" in e.detail
               for e in await h.event_list())


# -- discovery ------------------------------------------------------------------------------------

async def test_discovery_and_add_by_address(api, h, jane, pool, monkeypatch):
    from app import main
    monkeypatch.setattr(main.settings, "DISCOVERY_HOSTS", "jane, quiet")
    r = await api.post("/connected-agents/discover")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["looked_at"]["hosts"][:2] == ["jane", "quiet"] and d["found"] >= 1
    found = [x for x in d["discoveries"] if x["host"] == "jane" and x["status"] == "found"]
    assert found and found[0]["kind"] == "openclaw" and found[0]["skills"] == ["Jane"]
    # connect what was found; it is then marked
    r = await api.post("/connected-agents", json={"discovery_id": found[0]["id"]})
    assert r.status_code == 201, r.text
    listed = (await api.get("/connected-agents/discoveries")).json()["discoveries"]
    assert next(x for x in listed if x["id"] == found[0]["id"])["connected_agent_name"] == "Jane"
    # "Add by address": a bare host name tries the default ports
    r = await api.post("/connected-agents/probe", json={"address": "jane"})
    assert [(f["host"], f["port"]) for f in r.json()["found"]] == [("jane", 18789)]
    r = await api.post("/connected-agents/probe", json={"address": "ftp://x"})
    assert r.status_code == 422


# -- agents on an agent host (protocol runner) --------------------------------------------------------

async def test_agent_host_job_is_pinned_and_applied_like_remote_work(api, h, pool):
    h.engine.jobs.remote_poll_seconds = 0.02
    runner = await RunnerRepo(pool).create("fitz-host", hash_token("daash_rt_x"))
    token = mcp_server.new_connected_token()
    ca = await ConnectedAgentRepo(pool).create(
        "Fitz", "runner", None, kind="openclaw", runner_id=runner.id, local_ref="main",
        mcp_token_hash=hash_token(token), capabilities=["edit_files", "run_code"])
    cfg = connected_config(HarnessSpec(harness="connected", connected_agent=ca.id))
    team, agent, task = await swarm_with_task(h, cfg)
    assert agent.capabilities == ["edit_files", "run_code"]
    repo = RemoteJobRepo(pool)
    work = asyncio.create_task(h.engine.jobs.work_task(await h.live(), _work_action(task)))
    await h.wait_for(lambda: _has_job(repo, h), label="the pinned job")
    job = (await repo.list(h.swarm_id))[0]
    p = job.payload
    assert (job.harness, job.status, job.model) == ("connected", "queued", "")
    assert (p["connected_agent_id"], p["protocol"], p["runner_id"], p["relay"], p["local_ref"]) \
        == (ca.id, "runner", runner.id, "openclaw-agent", "main")
    assert p["prompt"] and p["system_prompt"] and "limits" in p
    # agent-host agents get DAASH's tools without the file tools
    r = await api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                       headers={"Authorization": f"Bearer {token}"})
    tools = {t["name"] for t in r.json()["result"]["tools"]}
    assert "remember" in tools and not tools & {"write_file", "read_file", "list_files"}
    # what the other side does: lease (pinned) and complete with a result
    await pool.execute("UPDATE remote_jobs SET status = 'leased', runner_id = $2, "
                       "leased_at = now(), lease_expires_at = now() + interval '1 hour' "
                       "WHERE id = $1", job.id, runner.id)
    await repo.complete(job.id, runner.id, {"outcome": "submitted", "summary": "Fitz did it",
                                            "files": [], "deleted": [], "usage": {}})
    await asyncio.wait_for(work, 10)
    t = await h.tasks.get(task.id)
    assert t.status == "Review"
    assert (t.deliverable["connected_agent"], t.deliverable["protocol"]) == ("Fitz", "runner")
    assert "model" not in t.deliverable
    assert any(e.detail.startswith("Delivery-1 (Fitz, on agent host 'fitz-host') submitted")
               for e in await h.event_list())


def _work_action(task):
    from app.models import Action, ActionKind
    return Action(kind=ActionKind.WORK_TASK, team_id=task.team_id, task_id=task.id,
                  agent_id=task.assigned_agent_id)


async def _has_job(repo, h):
    return bool(await repo.list(h.swarm_id))
