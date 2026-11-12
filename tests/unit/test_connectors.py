"""Connected-agent clients and discovery against stub servers (docs/DESIGN-0.4.md 5.3, 5.4, 5.8)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app import connectors as C
from app.connectors import ConnectorError
from tests.fakes.fake_a2a_agent import FakeA2AAgent


def transport(handler):
    return httpx.MockTransport(handler)


# -- addresses and the endpoint origin rule ------------------------------------------------------

def test_parse_address_forms():
    a = C.parse_address("jane-mac.tail1.ts.net")
    assert (a.scheme, a.host, a.port, a.path) == ("http", "jane-mac.tail1.ts.net", None, "")
    a = C.parse_address("jane:18789")
    assert a.origin == "http://jane:18789"
    a = C.parse_address("https://agents.example.org/.well-known/agent-card.json")
    assert (a.scheme, a.port, a.path) == ("https", None, "/.well-known/agent-card.json")
    assert a.origin == "https://agents.example.org"
    for bad in ("", "ftp://x", "http://user:pw@host:1", "http://ho st", "host:99999"):
        with pytest.raises(ValueError):
            C.parse_address(bad)


def test_card_urls():
    assert C.card_urls("http://jane:18789") == [
        "http://jane:18789/.well-known/agent-card.json", "http://jane:18789/.well-known/agent.json"]
    assert C.card_urls("http://jane:18789/custom/card.json") == ["http://jane:18789/custom/card.json"]


def test_endpoint_keeps_the_entered_origin_and_the_cards_path():
    info = C.parse_card(FakeA2AAgent("Jane", advertised="http://10.0.0.7:18789").card())
    assert info.rpc_url == "http://10.0.0.7:18789/a2a/v1"       # a LAN address DAASH can't reach
    assert C.endpoint_for("http://jane.tail1.ts.net:18789", info) == \
        "http://jane.tail1.ts.net:18789/a2a/v1"
    assert C.endpoint_for("jane.tail1.ts.net:18789", info, " http://proxy/a2a ") == "http://proxy/a2a"


def test_parse_card_shapes():
    one = C.parse_card({"name": "Gate", "supportedInterfaces": [
        {"url": "http://x/rpc/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
        {"url": "http://x/grpc", "protocolBinding": "GRPC"}], "skills": []})
    assert (one.rpc_path, one.protocol_version, one.kind) == ("/rpc/a2a", "1.0", "a2a")
    old = C.parse_card({"name": "Old", "url": "http://x:1/a2a/v1", "preferredTransport": "JSONRPC",
                        "protocolVersion": "0.3.0", "skills": [{"id": "s1", "name": "Sum"}]})
    assert old.rpc_path == "/a2a/v1" and old.skills == [{"id": "s1", "name": "Sum"}]
    oc = C.parse_card({"name": "My gateway", "description": "OpenClaw gateway", "skills": []})
    assert oc.kind == "openclaw" and oc.rpc_path == C.DEFAULT_RPC_PATH
    with pytest.raises(ConnectorError) as e:
        C.parse_card({"hello": "world"})
    assert e.value.kind == "protocol"


async def test_fetch_card_falls_back_to_agent_json_and_sends_no_credential():
    seen = []

    def handler(req):
        seen.append((req.url.path, req.headers.get("authorization")))
        if req.url.path == "/.well-known/agent.json":
            return httpx.Response(200, json={"name": "Legacy", "url": "http://a/a2a/v1"})
        return httpx.Response(404)

    card, url = await C.fetch_card("http://a:1", transport=transport(handler))
    assert card["name"] == "Legacy" and url.endswith("/agent.json")
    assert all(auth is None for _, auth in seen)


# -- the A2A client -------------------------------------------------------------------------------

async def test_check_classifies_answers():
    agent = FakeA2AAgent("Jane")
    t = transport(agent.httpx_handler)
    c = C.A2AClient("http://jane:18789/a2a/v1", "secret", transport=t)
    with pytest.raises(ConnectorError) as e:     # not approved yet: no peer token -> 401
        await c.check()
    assert e.value.kind == "token_rejected" and e.value.infrastructure
    agent.approve("secret")
    await c.check()                              # "task not found" = reachable, token accepted
    assert agent.requests[-1] == {"method": "GetTask", "auth": True}
    with pytest.raises(ConnectorError) as e:
        await C.A2AClient("http://jane:18789/a2a/v1", "wrong", transport=t).check()
    assert e.value.kind == "token_rejected"

    def down(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(ConnectorError) as e:
        await C.A2AClient("http://x/a2a/v1", "t", transport=transport(down)).check()
    assert e.value.kind == "unreachable"


@pytest.mark.parametrize("status,kind", [(429, "rate_limited"), (502, "server_error"),
                                         (404, "unreachable"), (400, "protocol")])
async def test_http_errors(status, kind):
    c = C.A2AClient("http://x/a2a/v1", "t",
                    transport=transport(lambda r: httpx.Response(status, text="no")))
    with pytest.raises(ConnectorError) as e:
        await c.get("t1")
    assert e.value.kind == kind


async def test_rate_limit_error_with_http_200():
    def handler(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {
            "code": -32029, "message": "Rate limit exceeded: 30 requests per minute"}})
    with pytest.raises(ConnectorError) as e:
        await C.A2AClient("http://x/a2a/v1", "t", transport=transport(handler)).get("t1")
    assert e.value.kind == "rate_limited"


async def test_send_and_poll_until_completed():
    agent = FakeA2AAgent("Jane", mode="blocks", token="tok")
    t = transport(agent.httpx_handler)
    sent = {}

    async def spy(req):
        if req.url.path == "/a2a/v1":
            sent.update(json.loads(req.content))
        return await agent.httpx_handler(req)

    c = C.A2AClient("http://jane:18789/a2a/v1", "tok", transport=transport(spy))
    task = await c.send("Task t1: Hello\n\nAcceptance criteria (each must be green):\n"
                        "1. a.md exists [file_exists path=a.md]\n", "daash-s1-t1-a0")
    assert task.state == "working" and task.id
    p = sent["params"]
    assert p["configuration"] == {"returnImmediately": True}
    assert p["message"]["role"] == "ROLE_USER" and p["message"]["contextId"] == "daash-s1-t1-a0"
    assert p["message"]["parts"][0]["text"].startswith("Task t1")
    for bg in agent.background:
        await bg
    done = await C.A2AClient("http://jane:18789/a2a/v1", "tok", transport=t).get(task.id)
    assert done.state == "completed" and "<<<FILE a.md>>>" in done.text


def test_task_from_result_shapes():
    failed = C.task_from_result({"task": {"id": "t", "status": {
        "state": "TASK_STATE_FAILED", "message": {"parts": [{"text": "no model"}]}}}})
    assert (failed.state, failed.text, failed.terminal) == ("failed", "no model", True)
    v03 = C.task_from_result({"kind": "task", "id": "t", "status": {"state": "completed"},
                              "artifacts": [{"parts": [{"kind": "text", "text": "hi"}]}]})
    assert (v03.state, v03.text) == ("completed", "hi")
    msg = C.task_from_result({"message": {"messageId": "m", "parts": [{"text": "direct"}]}})
    assert (msg.state, msg.text) == ("completed", "direct")
    assert C.normalize_state("TASK_STATE_INPUT_REQUIRED") == "input_required"
    with pytest.raises(ConnectorError):
        C.task_from_result({"task": {"status": {}}})


def test_context_id_format():
    assert C.context_id("5f0e1c2a-aaaa", "9b8c7d6e-ffff", 2) == "daash-5f0e1c2a-9b8c7d6e-a2"
    assert len(C.context_id("x" * 50, "y" * 50, 10)) <= 128


async def test_progress_helpers_map_states():
    agent = FakeA2AAgent("Jane", mode="fail", token="tok")
    ca = SimpleNamespace(protocol="a2a", endpoint="http://jane/a2a/v1", token="tok", url=None)
    t = transport(agent.httpx_handler)
    p = await C.start_work(ca, "do it", "daash-a-b-a0", transport=t)
    assert p.state == "working"
    for bg in agent.background:
        await bg
    p = await C.poll_work(ca, p.external_id, transport=t)
    assert p.state == "failed" and "always fails" in p.text
    assert await C.cancel_work(ca, p.external_id, transport=t) is False   # A2A cannot cancel


# -- Hermes API server ----------------------------------------------------------------------------

class StubHermes:
    """A stand-in for Hermes's API server with the run lifecycle of v0.23 (POST /v1/runs, GET
    /v1/runs/{id}, POST /v1/runs/{id}/stop, GET /v1/models, GET /health)."""

    def __init__(self, key="k"):
        self.key = key
        self.runs = {}
        self.calls = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path, req.headers.get("authorization")))
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "platform": "hermes-agent"})
        if req.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": {"message": "Invalid API key"}})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "hermes-agent"}]})
        if req.url.path == "/v1/runs" and req.method == "POST":
            body = json.loads(req.content)
            rid = f"run_{len(self.runs) + 1}"
            self.runs[rid] = {"status": "running", "input": body.get("input"),
                              "session_id": body.get("session_id"),
                              "idempotency_key": req.headers.get("idempotency-key")}
            return httpx.Response(202, json={"run_id": rid, "status": "started"})
        parts = req.url.path.split("/")
        if len(parts) >= 4 and parts[2] == "runs":
            run = self.runs.get(parts[3])
            if run is None:
                return httpx.Response(404, json={"error": "run not found"})
            if req.url.path.endswith("/stop"):
                run["status"] = "cancelled"
                return httpx.Response(200, json={"run_id": parts[3], "status": "stopping"})
            return httpx.Response(200, json={"run_id": parts[3], **run})
        return httpx.Response(404)


async def test_hermes_client_lifecycle():
    stub = StubHermes()
    t = transport(stub.handler)
    h = C.HermesClient("http://h:8642/v1", "k", transport=t)
    assert C.looks_like_hermes_health(await h.health())
    await h.check()
    run = await h.start("hello", "daash-a-b-a0")
    assert run.id == "run_1" and run.state == "running"
    assert stub.runs["run_1"]["session_id"] == "daash-a-b-a0"
    stub.runs["run_1"].update(status="completed", output="all done")
    got = await h.get("run_1")
    assert (got.state, got.text) == ("completed", "all done")
    await h.stop("run_1")
    assert stub.runs["run_1"]["status"] == "cancelled"
    with pytest.raises(ConnectorError) as e:
        await h.get("run_404")
    assert e.value.kind == "not_found"
    with pytest.raises(ConnectorError) as e:
        await C.HermesClient("http://h:8642", "bad", transport=t).check()
    assert e.value.kind == "token_rejected"
    assert stub.calls[0] == ("GET", "/health", None)     # no credential on the public probe


async def test_hermes_through_the_engine_interface():
    stub = StubHermes()
    t = transport(stub.handler)
    ca = SimpleNamespace(protocol="hermes-api", url="http://h:8642", token="k", endpoint=None)
    await C.check_agent(ca, transport=t)
    p = await C.start_work(ca, "x", "s", message_id="daash-job-1", transport=t)
    assert stub.runs[p.external_id]["idempotency_key"] == "daash-job-1"
    stub.runs[p.external_id].update(status="failed", error={"message": "model gone"})
    p = await C.poll_work(ca, p.external_id, transport=t)
    assert p.state == "failed" and p.text == "model gone"
    assert await C.cancel_work(ca, p.external_id, transport=t) is True


def test_run_from_json_variants():
    assert C.run_from_json({"id": "r", "status": "succeeded",
                            "output": [{"content": "a"}, "b"]}).text == "a\nb"
    assert C.run_from_json({"run_id": "r", "status": "queued"}).state == "running"
    assert C.run_from_json({"run_id": "r", "status": "stopped"}).state == "failed"


# -- discovery ------------------------------------------------------------------------------------

async def test_probe_finds_a2a_hermes_and_silent_ports():
    agent = FakeA2AAgent("Jane")
    stub = StubHermes()

    def handler(req):
        if req.url.host == "jane":
            return _sync(agent, req)
        if req.url.host == "herm":
            return stub.handler(req)
        if req.url.host == "fitz":
            raise httpx.ReadTimeout("silent")
        return httpx.Response(404)

    t = transport(handler)
    oc = await C.probe("jane", 18789, transport=t)
    assert (oc.kind, oc.status, oc.url) == ("openclaw", "found", "http://jane:18789")
    assert oc.name == "Jane's gateway" and "Jane" in oc.detail
    he = await C.probe("herm", 8642, transport=t)
    assert (he.kind, he.status) == ("hermes", "found")
    fz = await C.probe("fitz", 18789, transport=t)
    assert (fz.status, fz.detail) == ("silent", C.SILENT_DETAIL)
    assert await C.probe("web", 8642, transport=t) is None
    # never a credential while discovering
    assert all(auth is None for _, _, auth in stub.calls)


def _sync(agent, req):
    """FakeA2AAgent's GET answers without an event loop (for a sync MockTransport handler)."""
    if req.url.path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        return httpx.Response(200, json=agent.card())
    return httpx.Response(404)


async def test_probe_recognises_openclaw_before_its_a2a_channel_is_on():
    agent = FakeA2AAgent("Jane", channel_on=False)
    f = await C.probe("jane", 18789, transport=transport(agent.httpx_handler))
    assert (f.kind, f.status, f.card, f.url) == ("openclaw", "found", None, "http://jane:18789")
    assert "2026.11.3" in f.detail and "A2A channel is off" in f.detail
    assert C.openclaw_page_version("<html><p>hello</p>") is None


async def test_probe_skips_closed_ports():
    async def closed(host, port, timeout):
        return False
    assert await C.probe("nobody", 18789, tcp_open=closed) is None


async def test_discover_runs_all_pairs():
    t = transport(lambda r: _sync(FakeA2AAgent("A"), r) if r.url.port == 18789
                  else httpx.Response(404))
    found = await C.discover(C.targets(["a", "b:18789", "c"], [18789, 8642]), transport=t)
    assert sorted((f.host, f.port) for f in found) == [("a", 18789), ("b", 18789), ("c", 18789)]


def test_subnets_ports_and_targets():
    hosts, problems = C.expand_subnets("10.0.0.0/30, 192.168.1.0/16, 8.8.8.0/24, nope, fd00::/120")
    assert hosts == ["10.0.0.1", "10.0.0.2"]
    assert len(problems) == 4
    assert len(C.expand_subnets("10.1.2.0/24")[0]) == 254
    assert C.parse_ports("18789, 8642,x,0,8642") == [18789, 8642]
    assert C.parse_ports("") == list(C.DEFAULT_PORTS)
    assert C.targets(["jane", "fitz:9000", "jane"], [1, 2]) == [("jane", 1), ("jane", 2),
                                                               ("fitz", 9000)]


def test_stored_error_round_trip():
    e = ConnectorError("token_rejected", "HTTP 401")
    assert C.split_stored_error(e.stored()) == ("token_rejected", "HTTP 401")
    assert C.split_stored_error("plain") == (None, "plain")
    assert C.split_stored_error(None) == (None, None)
