"""End-to-end connected agent (docs/DESIGN-0.4.md sections 5 and 8): a scripted fake A2A agent
container (`fake-a2a`, no LLM) is found by discovery, connected, approved by its "owner" (the
same change DAASH's approval step makes, through the fake's control endpoint), noticed by the
engine, and then joins a fake-LLM swarm. It works through DAASH's MCP tools, including
`remember` and `send_message`, over the real network, and its task reaches Done."""
from __future__ import annotations

import os

import httpx

from tests.e2e.helpers import poll

FAKE_A2A_URL = os.environ.get("E2E_FAKE_A2A_URL", "http://fake-a2a:18789")


def test_connected_a2a_agent_joins_a_swarm_and_finishes_its_task(api, fake):
    control = httpx.Client(base_url=FAKE_A2A_URL, timeout=30)
    poll(lambda: control.get("/health").status_code == 200, timeout=60, desc="fake A2A agent")
    for old in api.get("/connected-agents")["connected_agents"]:
        api.delete(f"/connected-agents/{old['id']}")

    # discovery finds it (DAASH_DISCOVERY_HOSTS=fake-a2a), with no credential sent
    found = api.post("/connected-agents/discover")
    hits = [d for d in found["discoveries"] if d["host"] == "fake-a2a" and d["status"] == "found"]
    assert hits and hits[0]["kind"] == "openclaw" and hits[0]["port"] == 18789, found

    # connect: pending, with the owner's one step
    out = api.post("/connected-agents", {"discovery_id": hits[0]["id"], "name": "Jane"})
    agent, step = out["agent"], out["approval"]
    assert agent["status"] == "pending" and step["command"].startswith("openclaw config patch")
    assert step["mcp_url"] == "http://coordinator:8000/mcp"

    # the owner applies it; DAASH notices by itself (the engine checks pending agents)
    r = control.post("/_control/approve", json={"token": step["a2a_token"],
                                                "mcp_url": step["mcp_url"],
                                                "mcp_token": step["mcp_token"]})
    assert r.status_code == 200
    poll(lambda: api.get(f"/connected-agents/{agent['id']}")["status"] == "connected",
         timeout=60, desc="DAASH to notice the approval")

    cfg = {"allowed_harnesses": ["daash", "connected"], "delivery_agents_per_team": 1,
           "delivery_harnesses": [{"harness": "connected", "connected_agent": agent["id"],
                                   "model": ""}]}
    sid = api.create_swarm(config=cfg)["id"]
    api.transition(sid, "start")
    api.wait_verification(sid, timeout=240)

    tasks = api.tasks(sid)
    assert tasks and {t["status"] for t in tasks} == {"Done"}, api.dump(sid)
    via_jane = [t for t in tasks if t["deliverable"].get("connected_agent") == "Jane"]
    assert via_jane and all(t["deliverable"]["files"] for t in via_jane)
    details = [e["detail"] for e in api.events(sid)]
    for tool in ("write_file", "remember", "send_message"):
        assert any(f"(Jane) used DAASH tool {tool}" in d for d in details), tool
    assert any("Delivery-1 → coordinator: Jane here:" in d for d in details)
    assert any("(Jane, via A2A) submitted" in d for d in details)
    seen = api.get(f"/connected-agents/{agent['id']}")
    assert seen["daash_tools"]["in_use"] is True
    av = api.get("/availability")
    assert next(c for c in av["connected_agents"] if c["id"] == agent["id"])["availability"] \
        == "available"
    api.delete(f"/connected-agents/{agent['id']}")
