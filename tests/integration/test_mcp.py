"""DAASH 0.4 against the real database (docs/DESIGN-0.4.md sections 2, 3, 4.1 and 6): the MCP
server over HTTP with job and connected-agent tokens, the lease's MCP token, team messages in
prompts, and the agent-facing app."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app import agent_api, mcp_server, runner_api
from app.models import HarnessSpec
from app.remote import hash_token
from app.repo import ConnectedAgentRepo, MemoryRepo, MessageRepo, RemoteJobRepo, RunnerRepo
from tests.integration.helpers import swarm_config
from tests.integration.runner_helpers import FakeRunner, make_remote_job, new_runner

VISION = "Write a short guide to compost."


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch, h):
    monkeypatch.setattr(runner_api, "LEASE_POLL_SECONDS", 0.05)
    h.engine.jobs.remote_poll_seconds = 0.02


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def rpc(client, token: str, method: str, mid=1, **params) -> httpx.Response:
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if mid is not None:
        body["id"] = mid
    return await client.post("/mcp", json=body, headers=bearer(token))


async def tool(client, token: str, name: str, **arguments) -> tuple[str, bool]:
    r = await rpc(client, token, "tools/call", name=name, arguments=arguments)
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return res["content"][0]["text"], res["isError"]


async def leased_job(api, h, pool, delivery: int = 1, name: str = "r1"):
    """A swarm with an In-Progress task whose queued remote job a fake runner leased.
    Returns (runner, job JSON from the lease, task)."""
    await h.create_swarm(VISION, config=swarm_config(delivery), dod="- The guide exists")
    await h.transition("start")
    _, task = await make_remote_job(h, pool)
    _, token = await new_runner(api, name)
    fr = FakeRunner(api, token, name)
    assert (await fr.hello()).status_code == 200
    job = await fr.lease(0)
    assert job is not None
    return fr, job, task


# -- the lease's MCP token (section 4.1) ----------------------------------------------------------------

async def test_lease_returns_an_mcp_token_stored_only_as_a_hash(api, h, pool):
    _, job, _ = await leased_job(api, h, pool)
    mcp = job["mcp"]
    assert mcp["path"] == "/mcp" and mcp["token"].startswith("djt_")
    row = await pool.fetchrow("SELECT mcp_token_hash, payload::text AS p FROM remote_jobs "
                              "WHERE id = $1", job["id"])
    assert row["mcp_token_hash"] == hash_token(mcp["token"])
    assert mcp["token"] not in row["p"]
    assert (await RemoteJobRepo(pool).by_mcp_token_hash(hash_token(mcp["token"]))).id == job["id"]


async def test_a_job_token_can_remember_and_recall_then_dies_with_the_job(api, h, pool):
    fr, job, task = await leased_job(api, h, pool)
    token = job["mcp"]["token"]
    r = await rpc(api, token, "initialize", protocolVersion="2025-06-18", capabilities={},
                  clientInfo={"name": "claude-code", "version": "9"})
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-06-18"
    assert (await rpc(api, token, "notifications/initialized", mid=None)).status_code == 202
    names = [t["name"] for t in (await rpc(api, token, "tools/list")).json()["result"]["tools"]]
    assert "remember" in names and "send_message" in names and "write_file" not in names

    assert await tool(api, token, "remember", content="Compost needs browns and greens.") == \
        ("remembered", False)
    text, err = await tool(api, token, "recall", query="compost")
    assert not err and "browns and greens" in text
    (mem,) = await MemoryRepo(pool).list(task.assigned_agent_id)
    assert mem.task_id == task.id and mem.swarm_id == task.swarm_id
    text, err = await tool(api, token, "current_task")
    assert not err and text.startswith(f"Task {task.id}: Remote task")
    tool_events = await h.event_list("tool")
    assert len(tool_events) == 3 and tool_events[0].actor == task.assigned_agent_id
    assert "used DAASH tool remember" in tool_events[0].detail
    assert (await RemoteJobRepo(pool).get(job["id"])).payload["mcp_calls"] == 3

    assert await fr.post_result(job) == 200            # the job ends ...
    text, err = await tool(api, token, "remember", content="too late")
    assert err and text == mcp_server.JOB_ENDED         # ... and so does its token
    assert len(await MemoryRepo(pool).list(task.assigned_agent_id)) == 1


async def test_cancel_pause_and_requeue_end_a_job_token(api, h, pool, settings):
    _, job, _ = await leased_job(api, h, pool)
    token = job["mcp"]["token"]
    await h.transition("pause")
    assert await tool(api, token, "recall") == (mcp_server.JOB_ENDED, True)
    await h.transition("resume")
    assert (await tool(api, token, "recall"))[1] is False
    # the lease expires (runner vanished): requeued, and a new lease gets a new token
    await pool.execute("UPDATE remote_jobs SET lease_expires_at = now() - interval '1 minute' "
                       "WHERE id = $1", job["id"])
    await RemoteJobRepo(pool).requeue_expired(datetime.now(timezone.utc))
    assert await tool(api, token, "recall") == (mcp_server.JOB_ENDED, True)
    _, rtoken = await new_runner(api, "r2")
    fr2 = FakeRunner(api, rtoken, "r2")
    await fr2.hello()
    again = await fr2.lease(0)
    assert again["id"] == job["id"] and again["mcp"]["token"] != token
    assert (await rpc(api, token, "ping")).status_code == 401     # the old token is unknown now
    assert (await tool(api, again["mcp"]["token"], "recall"))[1] is False
    await RemoteJobRepo(pool).cancel(job["id"])
    assert await tool(api, again["mcp"]["token"], "recall") == (mcp_server.JOB_ENDED, True)


async def test_tokens_and_admin_auth(api, h, pool, settings):
    _, job, _ = await leased_job(api, h, pool)
    token = job["mcp"]["token"]
    assert (await api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            ).status_code == 401
    for bad in ("djt_" + "x" * 43, "dca_" + "x" * 43, "daash_rt_e2e-fixed-test-token"):
        r = await rpc(api, bad, "ping")
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert (await api.get("/mcp", headers=bearer(token))).status_code == 405
    settings.API_TOKEN = "admin-secret"         # the admin token guards the admin API only
    assert (await api.get("/swarms")).status_code == 401
    assert (await rpc(api, token, "ping")).json()["result"] == {}
    assert (await rpc(api, "admin-secret", "ping")).status_code == 401


# -- team messages over MCP (section 3) ------------------------------------------------------------------

async def test_send_and_read_messages_over_mcp(api, h, pool):
    _, job, task = await leased_job(api, h, pool, delivery=3)
    token = job["mcp"]["token"]
    team = (await h.team_list())[0]
    agents = {a.name: a for a in await h.agent_list(team)}
    me = next(a for a in agents.values() if a.id == task.assigned_agent_id)
    assert me.name == "Delivery-1"
    assert await tool(api, token, "send_message", to="delivery-2", text="I own r.md") == \
        ("message sent to Delivery-2", False)
    text, err = await tool(api, token, "send_message", to="team", text="Status: halfway")
    assert not err and "Delivery-2, Delivery-3" in text
    assert (await tool(api, token, "send_message", to="coordinator", text="Need a docs team"))[1] \
        is False
    text, err = await tool(api, token, "send_message", to="Quality Judgement", text="hi")
    assert err and "Delivery-2, Delivery-3, team, coordinator" in text
    msgs = MessageRepo(pool)
    d2 = await msgs.unread(agents["Delivery-2"].id)
    assert [(m.text, m.audience, m.from_name) for m in d2] == [
        ("I own r.md", "direct", "Delivery-1"), ("Status: halfway", "team", "Delivery-1")]
    assert [m.text for m in await msgs.unread(agents["Delivery-3"].id)] == ["Status: halfway"]
    coord = next(a for a in await h.agent_list() if a.role == "swarm_coordinator")
    (note,) = await msgs.unread(coord.id)
    assert note.from_team == "Team A" and note.audience == "coordinator"
    assert await msgs.sends_in_attempt(me.id, task.id, 0) == 3
    details = [e.detail for e in await h.event_list("message")]
    assert details == ["Delivery-1 → Delivery-2: I own r.md", "Delivery-1 → team: Status: halfway",
                       "Delivery-1 → coordinator: Need a docs team"]

    await msgs.send(task.swarm_id, team.id, agents["Delivery-2"].id, [me.id], "Fine by me.")
    text, err = await tool(api, token, "read_messages")
    assert not err and "From Delivery-2 (to you, " in text and text.endswith("Fine by me.")
    assert await tool(api, token, "read_messages") == ("(no unread messages)", False)
    roster, _ = await tool(api, token, "team_roster")
    assert "- Delivery-1 (you): Mission Delivery, agent type" in roster


async def test_message_rows_are_limited_and_cascade(pool, h):
    await h.create_swarm(VISION, config=swarm_config(2), dod="- x")
    team = await h.seed_team("Team A")
    d1, d2 = [a for a in await h.agent_list(team) if a.role == "mission_delivery"]
    msgs = MessageRepo(pool)
    sent = await msgs.send(team.swarm_id, team.id, d1.id, [d2.id, d2.id], "once", "team", "t", 1)
    assert len(sent) == 1                                      # one row per distinct recipient
    with pytest.raises(Exception):
        await msgs.send(team.swarm_id, team.id, d1.id, [d2.id], "x" * 4001)
    taken = await msgs.take_unread(d2.id)
    assert [m.text for m in taken] == ["once"] and taken[0].read_at is not None
    assert await msgs.take_unread(d2.id) == []
    assert [m.to_name for m in await msgs.list_for_swarm(team.swarm_id)] == ["Delivery-2"]
    await h.swarms.delete(team.swarm_id)
    assert await pool.fetchval("SELECT count(*) FROM agent_messages") == 0


# -- connected-agent tokens (sections 2.3 and 5) ----------------------------------------------------------

async def connected_job(h, pool, ca, task_name: str, agent_name: str = "Delivery-1"):
    team = (await h.team_list())[0]
    agent = next(a for a in await h.agent_list(team) if a.name == agent_name)
    task = await h.add_task(team, task_name, artifact=f"{task_name.lower()}.md",
                            status="In-Progress", assigned_agent_id=agent.id)
    job = await RemoteJobRepo(pool).ensure_active(
        task.swarm_id, team.id, task.id, agent.id, 0, "connected", "",
        {"connected_agent_id": ca.id, "protocol": "a2a", "prompt": "do it"})
    return job, task


async def test_connected_agent_token(api, h, pool, settings):
    await h.create_swarm(VISION, config=swarm_config(2), dod="- The guide exists")
    await h.seed_team("Team A")
    await h.transition("start")
    token = mcp_server.new_connected_token()
    jane = await ConnectedAgentRepo(pool).create("Jane", "a2a", "http://jane.local:18789",
                                                 token="peer-secret",
                                                 mcp_token_hash=hash_token(token))
    assert "token" not in jane.model_dump() and "mcp_token_hash" not in jane.model_dump()
    tools = (await rpc(api, token, "tools/list")).json()["result"]["tools"]
    assert tools[0]["name"] == "list_files" and "job" in tools[0]["inputSchema"]["properties"]
    text, err = await tool(api, token, "current_task")
    assert err and "no active DAASH job" in text

    job, task = await connected_job(h, pool, jane, "Intro")
    text, err = await tool(api, token, "write_file", path="intro.md", content="# Intro\nHello")
    assert not err
    ws = Path(settings.WORKSPACE_ROOT) / task.swarm_id
    assert (ws / "intro.md").read_text() == "# Intro\nHello"
    assert (await tool(api, token, "read_file", path="intro.md"))[0] == "# Intro\nHello"
    assert (await tool(api, token, "write_file", path=".daash/x", content="no"))[1] is True
    row = await RemoteJobRepo(pool).get(job.id)
    assert row.payload["mcp_files"] == ["intro.md"] and row.payload["mcp_calls"] == 3
    assert (await ConnectedAgentRepo(pool).get(jane.id)).mcp_seen_at is not None
    assert "Delivery-1 (Jane) used DAASH tool write_file" in (await h.event_list("tool"))[0].detail

    job2, task2 = await connected_job(h, pool, jane, "Body", agent_name="Delivery-2")
    text, err = await tool(api, token, "remember", content="x")
    assert err and f"2 active DAASH jobs ({job.id}, {job2.id})" in text
    text, err = await tool(api, token, "current_task", job=job2.id)
    assert not err and text.startswith(f"Task {task2.id}: Body")
    # connected jobs are never leased by runners nor requeued
    await pool.execute("UPDATE remote_jobs SET status = 'leased', lease_expires_at = "
                       "now() - interval '1 hour' WHERE id = $1", job2.id)
    assert await RemoteJobRepo(pool).requeue_expired(datetime.now(timezone.utc)) == 0
    await pool.execute("UPDATE remote_jobs SET status = 'queued' WHERE id = $1", job2.id)
    sneaky, _ = await new_runner(api, "sneaky")
    runner = await RunnerRepo(pool).get(sneaky["id"])
    assert await RemoteJobRepo(pool).lease_next(runner, {"connected": []},
                                                datetime.now(timezone.utc)) is None
    await pool.execute("UPDATE remote_jobs SET status = 'completed' WHERE id = $1", job2.id)
    assert await tool(api, token, "remember", content="x", job=job2.id) == \
        (mcp_server.JOB_ENDED, True)
    assert (await tool(api, token, "remember", content="x"))[1] is False   # job 1 only now
    await ConnectedAgentRepo(pool).update(jane.id, enabled=False)
    assert (await rpc(api, token, "ping")).status_code == 401


# -- prompts carry unread messages, which are then read ---------------------------------------------------

async def test_native_work_prompt_has_messages_and_the_agent_can_send(h, fake, pool):
    fake.configure({"record_prompts": True, "work_message": "Taking the guide."})
    await h.create_swarm(VISION, config=swarm_config(2), dod="- The guide exists")
    team = await h.seed_team("Team A")
    agents = {a.name: a for a in await h.agent_list(team)}
    task = await h.add_task(team, "Guide", artifact="guide.md", status="In-Progress",
                            assigned_agent_id=agents["Delivery-1"].id, complexity="moderate")
    msgs = MessageRepo(pool)
    await msgs.send(task.swarm_id, team.id, agents["Delivery-2"].id, [agents["Delivery-1"].id],
                    "Use British spelling.")
    await h.transition("start")

    async def submitted() -> bool:
        return (await h.tasks.get(task.id)).status in ("Review", "Done")

    await h.run_until(submitted, label="the task is submitted")
    work = fake.calls_for("work")[0]
    assert "Messages from your team (now marked read):\n- Delivery-2 to you: Use British " \
           "spelling." in work["user"]
    assert await msgs.unread(agents["Delivery-1"].id) == []
    # the built-in agent used send_message (scenario work_message) to reach Delivery-2
    (note,) = await msgs.unread(agents["Delivery-2"].id)
    assert note.text == "Taking the guide." and note.audience == "team"
    assert "Delivery-1 → team: Taking the guide." in [e.detail for e in
                                                      await h.event_list("message")]


async def test_remote_job_prompt_has_messages_marked_read_when_the_job_is_created(h, pool):
    cfg = swarm_config(2)
    cfg.allowed_harnesses = ["daash", "fake"]
    cfg.delivery_harnesses = [HarnessSpec(harness="fake", model="")]
    await h.create_swarm(VISION, config=cfg, dod="- The guide exists")
    team = await h.seed_team("Team A")
    agents = {a.name: a for a in await h.agent_list(team)}
    task = await h.add_task(team, "Guide", artifact="guide.md", status="In-Progress",
                            assigned_agent_id=agents["Delivery-1"].id, complexity="moderate")
    msgs = MessageRepo(pool)
    await msgs.send(task.swarm_id, team.id, agents["Delivery-2"].id, [agents["Delivery-1"].id],
                    "Headings in sentence case, please.")
    await h.transition("start")
    deadline = datetime.now(timezone.utc) + timedelta(seconds=15)
    job = None
    while job is None:
        assert datetime.now(timezone.utc) < deadline, "no remote job was queued"
        await h.engine.tick()
        job = await RemoteJobRepo(pool).latest_for_task(task.id)
    prompt = job.payload["prompt"]
    assert prompt.startswith("Job: do this task.")
    assert "You have DAASH tools (MCP server `daash`)" in prompt
    assert "- Delivery-2 to you: Headings in sentence case, please." in prompt
    assert await msgs.unread(agents["Delivery-1"].id) == []
    await h.close()


async def test_vision_review_reads_messages_to_the_coordinator(h, fake, pool):
    fake.configure({"tasks_per_plan": 1, "record_prompts": True})
    await h.create_swarm(VISION, config=swarm_config(1), dod="- The guide exists")
    team = await h.seed_team("Team A")
    d1 = next(a for a in await h.agent_list(team) if a.role == "mission_delivery")
    coord = next(a for a in await h.agent_list() if a.role == "swarm_coordinator")
    msgs = MessageRepo(pool)
    await msgs.send(team.swarm_id, team.id, d1.id, [coord.id], "The guide lacks a glossary.",
                    audience="coordinator")
    await h.transition("start")
    await h.run_to_state("VERIFICATION")
    (call,) = fake.calls_for("review_vision")
    assert "Messages the teams sent to you, the Swarm Coordinator" in call["user"]
    assert "- Delivery-1 (team 'Team A'): The guide lacks a glossary." in call["user"]
    assert await msgs.unread(coord.id) == []


# -- the agent-facing app (section 6) ---------------------------------------------------------------------

@pytest_asyncio.fixture
async def agent_client(pool, bus, settings, monkeypatch):
    monkeypatch.setattr(agent_api, "pool", pool)
    monkeypatch.setattr(agent_api, "bus", bus)
    monkeypatch.setattr(agent_api, "settings", settings)
    monkeypatch.setattr(agent_api, "embedder", agent_api.build_embedder("none", False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_api.app),
                                 base_url="http://agents") as client:
        yield client


async def test_agent_api_serves_only_health_mcp_and_runner(agent_client, api, h, pool, settings):
    settings.API_TOKEN = "admin-secret"
    assert (await agent_client.get("/health")).json() == {"status": "ok", "service": "agent-api"}
    for path in ("/swarms", "/runners", "/config", "/help/prompts", "/docs", "/openapi.json",
                 "/redoc", "/remote-jobs", "/agent-endpoint", "/availability"):
        for headers in ({}, bearer("admin-secret")):
            assert (await agent_client.get(path, headers=headers)).status_code == 404, path
    assert (await agent_client.post("/runners", json={"name": "x"},
                                    headers=bearer("admin-secret"))).status_code in (404, 405)
    # a runner works entirely through the agent port: hello, lease (with MCP token), MCP, result
    await h.create_swarm(VISION, config=swarm_config(1), dod="- The guide exists")
    await h.transition("start")
    await make_remote_job(h, pool)
    _, rtoken = await new_runner(api, "far-away", admin=bearer("admin-secret"))
    fr = FakeRunner(agent_client, rtoken, "far-away")
    assert (await fr.hello()).status_code == 200
    job = await fr.lease(0)
    assert job["mcp"]["path"] == "/mcp"
    assert await tool(agent_client, job["mcp"]["token"], "remember", content="via 8001") == \
        ("remembered", False)
    assert await fr.post_result(job) == 200
    assert (await tool(agent_client, job["mcp"]["token"], "recall"))[1] is True
    assert (await agent_client.post("/runner/hello", json={})).status_code == 401


async def test_agent_endpoint_info_for_daashboard(api, settings):
    body = (await api.get("/agent-endpoint")).json()
    assert body["configured"] is False and body["public_url"] is None and body["port"] == 8001
    assert "DAASH_AGENT_PUBLIC_URL" in body["how_to_set"]
    settings.AGENT_PUBLIC_URL = "http://m5m128gb.tail391a32.ts.net:8001/"
    body = (await api.get("/agent-endpoint")).json()
    assert body["configured"] and body["mcp_url"] == "http://m5m128gb.tail391a32.ts.net:8001/mcp"
    assert body["runner_url"] == "http://m5m128gb.tail391a32.ts.net:8001"
