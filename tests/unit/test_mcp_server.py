"""The DAASH MCP server (docs/DESIGN-0.4.md section 2): tokens, the JSON-RPC handler, the
backend's identities and job checks (against in-memory repositories), the HTTP router, and the
lease's MCP token (section 4.1)."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app import mcp_server as M
from app import runner_api
from app.models import ConnectedAgent, RemoteJob
from app.remote import hash_token
from tests.unit.agent_fakes import T0, World


# -- tokens and pure rules ----------------------------------------------------------------------------

def test_tokens():
    job, conn = M.new_job_token(), M.new_connected_token()
    assert job.startswith("djt_") and len(job) == 4 + 43
    assert conn.startswith("dca_") and len(conn) == 4 + 43
    assert M.new_job_token() != job
    assert M.token_kind(job) == "job" and M.token_kind(conn) == "connected"
    for other in ("", "djt_", "daash_rt_x", "Bearer djt_x", "dca"):
        assert M.token_kind(other) is None


def test_protocol_version_negotiation():
    for v in M.PROTOCOL_VERSIONS:
        assert M.negotiate_version(v) == v
    assert M.negotiate_version("2099-01-01") == M.PROTOCOL_VERSIONS[0]
    assert M.negotiate_version(None) == M.PROTOCOL_VERSIONS[0]


def test_job_ended_rules():
    ok = ("ACTIVE", "In-Progress")
    assert not M.job_ended("job", "leased", *ok)
    for status in ("queued", "completed", "cancelled", "abandoned"):
        assert M.job_ended("job", status, *ok), status        # requeued counts as ended too
    assert not M.job_ended("connected", "queued", *ok)
    assert not M.job_ended("connected", "leased", *ok)
    assert M.job_ended("connected", "completed", *ok)
    assert M.job_ended("job", "leased", "PAUSED", "In-Progress")
    assert M.job_ended("job", "leased", "ACTIVE", "Review")
    assert M.job_ended("job", "leased", None, None)


def test_pick_connected_job():
    assert M.pick_connected_job(["j1"], None) == ("j1", None)
    assert M.pick_connected_job(["j1", "j2"], "j2") == ("j2", None)
    jid, msg = M.pick_connected_job(["j1", "j2"], None)
    assert jid is None and "2 active DAASH jobs (j1, j2)" in msg and "job=<id>" in msg
    jid, msg = M.pick_connected_job([], None)
    assert jid is None and "no active DAASH job" in msg and "Active jobs: none" in msg
    jid, msg = M.pick_connected_job(["j1"], "j9")
    assert jid is None and "j9 is not one of your active DAASH jobs (active: j1)" in msg


def test_tool_lists_per_token_kind():
    job = M.mcp_tools(workspace=False, job_arg=False)
    names = [t["name"] for t in job]
    assert "remember" in names and "send_message" in names and "current_task" in names
    assert "write_file" not in names and "submit" not in names
    assert all("job" not in t["inputSchema"]["properties"] for t in job)
    conn = M.mcp_tools(workspace=True, job_arg=True)
    assert [t["name"] for t in conn][:5] == ["list_files", "read_file", "write_file",
                                             "append_file", "delete_file"]
    assert all(t["inputSchema"]["properties"]["job"]["type"] == "string" for t in conn)
    assert all("job" not in t["inputSchema"].get("required", []) for t in conn)
    # each listing has its own copy of the schemas
    assert all("job" not in t["inputSchema"]["properties"]
               for t in M.mcp_tools(workspace=False, job_arg=False))


# -- the JSON-RPC handler, with a stub backend ---------------------------------------------------------

class StubBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def identify(self, token):
        return M.Identity("job", job_id="j1") if token == "djt_good" else None

    def list_tools(self, identity):
        return [{"name": "remember", "description": "d", "inputSchema": {"type": "object"}}]

    async def call_tool(self, identity, name, arguments):
        self.calls.append((name, arguments))
        if name == "boom":
            raise RuntimeError("db down")
        if name == "bad":
            return "Error: nope", True
        return f"ran {name}", False


def rpc(method, mid=1, **params):
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    if mid is not None:
        msg["id"] = mid
    return json.dumps(msg).encode()


async def test_auth_comes_first():
    h = M.McpHandler(StubBackend())
    for token in ("", "djt_wrong"):
        status, body = await h.handle_http(token, rpc("ping"))
        assert status == 401 and "token" in body["detail"]
    status, _ = await h.handle_http("djt_wrong", b"not json")
    assert status == 401


async def test_initialize_and_notifications():
    h = M.McpHandler(StubBackend())
    status, body = await h.handle_http("djt_good", rpc("initialize", protocolVersion="2025-06-18",
                                                       capabilities={},
                                                       clientInfo={"name": "c", "version": "1"}))
    assert status == 200 and body["id"] == 1 and body["jsonrpc"] == "2.0"
    res = body["result"]
    assert res["protocolVersion"] == "2025-06-18"
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"]["name"] == "daash" and "remember/recall" in res["instructions"]
    status, body = await h.handle_http("djt_good", rpc("initialize", mid="a",
                                                       protocolVersion="1999-01-01"))
    assert body["id"] == "a" and body["result"]["protocolVersion"] == M.PROTOCOL_VERSIONS[0]
    assert await h.handle_http("djt_good", rpc("notifications/initialized", mid=None)) == (202, None)
    assert await h.handle_http("djt_good", rpc("notifications/cancelled", mid=None,
                                               requestId=3)) == (202, None)
    # a client's response to a server request needs no answer either
    assert await h.handle_http("djt_good", json.dumps(
        {"jsonrpc": "2.0", "id": 5, "result": {}}).encode()) == (202, None)


async def test_ping_list_and_call():
    backend = StubBackend()
    h = M.McpHandler(backend)
    assert (await h.handle_http("djt_good", rpc("ping")))[1]["result"] == {}
    tools = (await h.handle_http("djt_good", rpc("tools/list", cursor=None)))[1]["result"]["tools"]
    assert tools[0]["name"] == "remember"
    _, body = await h.handle_http("djt_good", rpc("tools/call", name="remember",
                                                  arguments={"content": "x"}))
    assert body["result"] == {"content": [{"type": "text", "text": "ran remember"}],
                              "isError": False}
    _, body = await h.handle_http("djt_good", rpc("tools/call", name="recall"))
    assert body["result"]["isError"] is False and backend.calls[-1] == ("recall", {})


async def test_tool_errors_are_results_not_http_errors():
    h = M.McpHandler(StubBackend())
    status, body = await h.handle_http("djt_good", rpc("tools/call", name="bad", arguments={}))
    assert status == 200 and body["result"]["isError"] is True
    assert body["result"]["content"][0]["text"] == "Error: nope"
    status, body = await h.handle_http("djt_good", rpc("tools/call", name="boom", arguments={}))
    assert status == 200 and body["result"]["isError"] is True
    assert "could not run boom" in body["result"]["content"][0]["text"]


async def test_protocol_errors():
    h = M.McpHandler(StubBackend())
    status, body = await h.handle_http("djt_good", b"{nope")
    assert status == 400 and body["error"]["code"] == M.JSONRPC_PARSE_ERROR
    _, body = await h.handle_http("djt_good", rpc("resources/subscribe"))
    assert body["error"]["code"] == M.JSONRPC_METHOD_NOT_FOUND
    _, body = await h.handle_http("djt_good", json.dumps({"id": 1, "method": "ping"}).encode())
    assert body["error"]["code"] == M.JSONRPC_INVALID_REQUEST
    _, body = await h.handle_http("djt_good", rpc("tools/call", arguments={}))
    assert body["error"]["code"] == M.JSONRPC_INVALID_PARAMS
    _, body = await h.handle_http("djt_good", rpc("tools/call", name="x", arguments=[1]))
    assert body["error"]["code"] == M.JSONRPC_INVALID_PARAMS
    _, body = await h.handle_http("djt_good", json.dumps(
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": [1]}).encode())
    assert body["error"]["code"] == M.JSONRPC_INVALID_PARAMS
    status, _ = await h.handle_http("djt_good", b"[]")
    assert status == 400
    status, _ = await h.handle_http("djt_good", b" " * (M.MAX_BODY_BYTES + 1))
    assert status == 413


async def test_batches_and_empty_listings():
    h = M.McpHandler(StubBackend())
    batch = b"[" + b",".join([rpc("ping", 1), rpc("notifications/initialized", None),
                              rpc("prompts/list", 2), rpc("resources/list", 3)]) + b"]"
    status, body = await h.handle_http("djt_good", batch)
    assert status == 200 and [r["id"] for r in body] == [1, 2, 3]
    assert body[1]["result"] == {"prompts": []} and body[2]["result"] == {"resources": []}
    only_notes = b"[" + rpc("notifications/initialized", None) + b"]"
    assert await h.handle_http("djt_good", only_notes) == (202, None)


# -- the backend: identities, job checks, recording ---------------------------------------------------

def connected_agent(w: World, token: str, enabled: bool = True) -> ConnectedAgent:
    a = ConnectedAgent(id="ca-jane", name="Jane", protocol="a2a", url="http://jane:18789",
                       mcp_token_hash=hash_token(token), enabled=enabled, created_at=T0)
    w.repos.connected.by_id[a.id] = a
    return a


def backend(w: World, tmp_path: Path | None = None) -> M.DaashBackend:
    return M.DaashBackend(w.repos, workspace_root=(lambda sid: tmp_path) if tmp_path else None)


async def test_job_token_identity_and_a_call_that_is_recorded():
    w = World()
    token = M.new_job_token()
    job = w.job("job-1", status="leased")
    w.repos.remote_jobs.hashes[hash_token(token)] = job.id
    b = backend(w)
    ident = await b.identify(token)
    assert ident == M.Identity("job", job_id="job-1")
    assert await b.identify(M.new_job_token()) is None
    assert await b.identify("daash_rt_" + token[4:]) is None
    text, err = await b.call_tool(ident, "remember", {"content": "the sieve is fastest"})
    assert (text, err) == ("remembered", False)
    (mem,) = w.repos.memories.rows
    assert mem.agent_id == "a-d1" and mem.task_id == "t1"
    (ev,) = w.repos.events.of("tool")
    assert ev.actor == "a-d1" and ev.task_id == "t1" and ev.team_id == "tm1"
    assert ev.detail == "Delivery-1 used DAASH tool remember (content=<20 characters>)."
    assert w.repos.remote_jobs.by_id["job-1"].payload["mcp_calls"] == 1
    # the job id may be named, but only this token's
    assert (await b.call_tool(ident, "recall", {"job": "job-1"}))[1] is False
    text, err = await b.call_tool(ident, "recall", {"job": "job-2"})
    assert err and "belongs to DAASH job job-1" in text
    text, err = await b.call_tool(ident, "nope", {})
    assert err and "unknown tool 'nope'" in text and "send_message" in text
    assert "it failed" in w.repos.events.of("tool")[-1].detail


@pytest.mark.parametrize("change", ["completed", "cancelled", "abandoned", "queued", "paused",
                                    "review"])
async def test_job_token_stops_working_when_the_job_ends(change):
    w = World()
    job = w.job("job-1", status="leased")
    ident = M.Identity("job", job_id=job.id)
    b = backend(w)
    assert (await b.call_tool(ident, "remember", {"content": "x"}))[1] is False
    if change == "paused":
        w.repos.swarms.by_id["s1"] = w.swarm.model_copy(update={"lifecycle_state": "PAUSED"})
    elif change == "review":
        w.repos.tasks.by_id["t1"] = w.task.model_copy(update={"status": "Review"})
    else:
        w.repos.remote_jobs.set_status(job.id, change)
    events = len(w.repos.events.events)
    text, err = await b.call_tool(ident, "remember", {"content": "y"})
    assert err and text == M.JOB_ENDED
    assert len(w.repos.memories.rows) == 1 and len(w.repos.events.events) == events


async def test_connected_token_resolves_to_the_agents_active_jobs(tmp_path):
    w = World()
    token = M.new_connected_token()
    jane = connected_agent(w, token)
    b = backend(w, tmp_path)
    ident = await b.identify(token)
    assert ident.kind == "connected" and ident.connected.id == "ca-jane"
    assert [t["name"] for t in b.list_tools(ident)][:1] == ["list_files"]
    # no active job yet
    text, err = await b.call_tool(ident, "current_task", {})
    assert err and "no active DAASH job" in text
    w.job("cj-1", status="queued", harness="connected", connected_agent_id=jane.id)
    text, err = await b.call_tool(ident, "current_task", {})
    assert not err and text.endswith("DAASH job: cj-1")
    text, err = await b.call_tool(ident, "write_file", {"path": "jane.md", "content": "hi"})
    assert not err and (tmp_path / "jane.md").read_text() == "hi"
    assert w.repos.remote_jobs.by_id["cj-1"].payload["mcp_files"] == ["jane.md"]
    assert w.repos.connected.seen == ["ca-jane", "ca-jane"]
    assert "Delivery-1 (Jane) used DAASH tool write_file" in w.repos.events.of("tool")[-1].detail
    # a second job: now the agent must say which
    w.job("cj-2", status="leased", harness="connected", agent=w.d2, connected_agent_id=jane.id)
    text, err = await b.call_tool(ident, "remember", {"content": "x"})
    assert err and "2 active DAASH jobs (cj-1, cj-2)" in text
    text, err = await b.call_tool(ident, "remember", {"content": "x", "job": "cj-2"})
    assert not err and w.repos.memories.rows[-1].agent_id == "a-d2"
    # a job of this agent that ended -> the job-ended message
    w.repos.remote_jobs.set_status("cj-2", "cancelled")
    text, err = await b.call_tool(ident, "remember", {"content": "x", "job": "cj-2"})
    assert err and text == M.JOB_ENDED
    # someone else's job is not offered
    w.job("cj-3", status="leased", harness="connected", connected_agent_id="ca-fitz")
    text, err = await b.call_tool(ident, "remember", {"content": "x", "job": "cj-3"})
    assert err and "not one of your active DAASH jobs (active: cj-1)" in text
    text, err = await b.call_tool(ident, "remember", {"content": "x", "job": 7})
    assert err and "'job' must be" in text


async def test_disabled_connected_agent_is_unknown():
    w = World()
    token = M.new_connected_token()
    connected_agent(w, token, enabled=False)
    assert await backend(w).identify(token) is None


# -- HTTP -----------------------------------------------------------------------------------------------

class _State:
    pool = object()
    settings = None


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setattr(M, "backend_for", lambda state: StubBackend())
    app = FastAPI()
    app.include_router(M.build_router(_State()))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_http_router(http):
    async with http as client:
        r = await client.post("/mcp", content=rpc("ping"))
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        auth = {"Authorization": "Bearer djt_good", "Accept": "application/json, text/event-stream"}
        r = await client.post("/mcp", content=rpc("ping"), headers=auth)
        assert r.status_code == 200 and r.headers["content-type"] == "application/json"
        assert r.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
        assert "mcp-session-id" not in r.headers                    # stateless
        r = await client.post("/mcp", content=rpc("notifications/initialized", None),
                              headers=auth)
        assert r.status_code == 202 and r.content == b""
        for method in ("get", "delete"):
            r = await getattr(client, method)("/mcp", headers=auth)
            assert r.status_code == 405 and r.headers["allow"] == "POST"


# -- the lease carries the job's MCP token (section 4.1) -------------------------------------------------

def test_job_for_runner_carries_the_mcp_token_once():
    job = RemoteJob(id="j1", swarm_id="s", team_id="t", task_id="k", agent_id="a", attempt=0,
                    harness="codex", model="fast", status="leased", created_at=T0,
                    payload={"prompt": "p", "mcp_calls": 3, "mcp_files": ["a.md"]})
    out = runner_api.job_for_runner(job, "djt_abc")
    assert out["mcp"] == {"path": "/mcp", "token": "djt_abc"}
    assert out["prompt"] == "p" and "mcp_calls" not in out and "mcp_files" not in out
    assert out["workspace_url"] == "/runner/jobs/j1/workspace" and out["id"] == "j1"
    assert "mcp" not in runner_api.job_for_runner(job)
