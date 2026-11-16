"""Agent hosts on the coordinator (docs/DESIGN-0.4.md 5.9): connect codes, the owner command, what a
hello's `agents` may contain, names, local refs, status, and MCP for hosted agents."""
from datetime import datetime, timedelta, timezone

from app import agent_api, agent_hosts as A, mcp_server
from app.mcp_server import Identity
from app.models import ConnectedAgent

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def test_codes_and_owner_command():
    a, b = A.new_enroll_code(), A.new_enroll_code()
    assert a != b and a.startswith("dre_") and len(a) == 4 + 24
    assert A.hash_code(a) == A.hash_code(f"  {a}\n") != a           # pasted with spaces: same code
    assert A.owner_command("http://m5m128gb.tail391a32.ts.net:8001/", a) == (
        "curl -fsSLo ~/daash-runner.pyz http://m5m128gb.tail391a32.ts.net:8001/runner/daash-runner.pyz"
        f" && python3 ~/daash-runner.pyz connect http://m5m128gb.tail391a32.ts.net:8001 {a}")


def test_enrollment_status():
    pending = {"used_at": None, "expires_at": NOW + timedelta(minutes=1)}
    assert A.enrollment_status(pending, NOW) == "pending"
    assert A.enrollment_status({**pending, "expires_at": NOW}, NOW) == "expired"
    assert A.enrollment_status({**pending, "used_at": NOW}, NOW) == "used"


def test_normalize_reported_agents():
    raw = [
        {"kind": "openclaw", "ref": "main", "name": "Fitz", "available": True, "detail": "ok",
         "tools": True},
        {"kind": "openclaw", "ref": "main", "name": "dup"},                  # duplicate: dropped
        {"kind": "hermes", "ref": " default ", "available": False, "new_token": True},
        {"kind": "a2a", "ref": "x"}, {"kind": "hermes"}, "junk", None,
        {"kind": "hermes", "ref": "p\x00rofile", "name": "N" * 200, "detail": "d" * 900},
    ]
    out = A.normalize_reported_agents(raw)
    assert out[0] == {"kind": "openclaw", "ref": "main", "name": "Fitz", "available": True,
                      "detail": "ok", "tools": True, "new_token": False}
    assert out[1] == {"kind": "hermes", "ref": "default", "name": "default", "available": False,
                      "detail": "", "tools": False, "new_token": True}
    assert out[2]["ref"] == "p rofile" and len(out[2]["name"]) == 80 and len(out[2]["detail"]) == 500
    assert len(out) == 3
    assert A.normalize_reported_agents("nope") == []
    many = [{"kind": "hermes", "ref": f"p{i}"} for i in range(80)]
    assert len(A.normalize_reported_agents(many)) == A.MAX_REPORTED_AGENTS


def test_names_and_local_refs():
    assert A.unique_name("Fitz", "m3u512gb", []) == "Fitz"
    assert A.unique_name("Fitz", "m3u512gb", ["fitz"]) == "Fitz (m3u512gb)"
    assert A.unique_name("Fitz", "m3u512gb", ["Fitz", "Fitz (m3u512gb)"]) == "Fitz (m3u512gb) 2"
    assert A.unique_runner_name("m3u512gb", ["m3u512gb", "m3u512gb-2"]) == "m3u512gb-3"
    assert A.unique_runner_name("", []) == "agent-host"
    # OpenClaw reports its agent id when no identity name is set: not "main" in DAASH
    assert A.hosted_name("main", "openclaw", "m3u512gb.tail391a32.ts.net") == "OpenClaw (m3u512gb)"
    assert A.hosted_name("Fitz", "openclaw", "m3u512gb") == "Fitz"
    assert A.hosted_name("Hermes", "hermes", "m3u512gb") == "Hermes"
    assert A.hosted_name("", "openclaw", "") == "OpenClaw"
    assert A.split_local_ref("main") == (None, "main")
    assert A.split_local_ref("hermes:main") == ("hermes", "main")
    assert A.split_local_ref("weird:main") == (None, "weird:main")


def test_runner_agent_view_and_effective_status():
    row = {"id": "a1", "name": "Fitz", "protocol": "runner", "kind": "openclaw",
           "status": "connected", "runner_id": "r1", "local_ref": "main",
           "capabilities": ["edit_files", "run_code"], "max_jobs": 1, "enabled": True,
           "card": {"host": "m3u512gb", "detail": "gateway live", "available": True,
                    "reported_name": "Fitz", "tools": True},
           "last_ok_at": NOW, "last_error": None, "mcp_seen_at": None, "created_at": NOW}
    v = A.runner_agent_view(row)
    assert v["relay"] == "openclaw-agent" and v["ref"] == "main" and v["host"] == "m3u512gb"
    assert v["tools"] is True and v["last_ok_at"] == NOW.isoformat() and "card" not in v
    assert A.effective_status("connected", True, NOW - timedelta(seconds=30), NOW) == "connected"
    assert A.effective_status("connected", True, NOW - timedelta(minutes=10), NOW) == "offline"
    assert A.effective_status("connected", False, NOW, NOW) == "offline"
    assert A.effective_status("connected", True, None, NOW) == "offline"


def _agent(protocol: str) -> ConnectedAgent:
    return ConnectedAgent(id="c1", name="Fitz", protocol=protocol, url=None, created_at=NOW)


def test_hosted_agents_get_tools_but_not_file_tools():
    hosted = Identity("connected", connected=_agent("runner"))
    direct = Identity("connected", connected=_agent("a2a"))
    has = mcp_server.has_file_tools
    assert not has(hosted) and has(direct) and not has(Identity("job", job_id="j"))
    backend = mcp_server.DaashBackend(repos=None)
    hosted_tools = {t["name"] for t in backend.list_tools(hosted)}
    assert "remember" in hosted_tools and "send_message" in hosted_tools
    assert not hosted_tools & {"list_files", "read_file", "write_file", "append_file", "delete_file"}
    assert "write_file" in {t["name"] for t in backend.list_tools(direct)}
    assert all("job" in t["inputSchema"]["properties"] for t in backend.list_tools(hosted))
    assert "write_file" not in mcp_server.instructions(hosted)
    assert "job=<id>" in mcp_server.instructions(hosted)


async def test_agent_port_routes():
    import httpx

    # no lifespan: no database behind it
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_api.app),
                                 base_url="http://agents") as client:
        assert (await client.post("/runner/enroll", json={})).status_code == 422   # it exists
        assert (await client.post("/runner/disconnect")).status_code == 503        # needs the DB
        assert (await client.post("/runner-enrollments")).status_code in (404, 405)  # admin only
        assert (await client.get("/runner-enrollments")).status_code == 404
