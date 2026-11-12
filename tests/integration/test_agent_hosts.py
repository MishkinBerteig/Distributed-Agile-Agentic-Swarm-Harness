"""Agent hosts against the real database (docs/DESIGN-0.4.md 5.9): connect codes, enrollment, the
runner zipapp, hello with local agents (upserted as `runner` connected agents, MCP tokens once),
jobs pinned to one runner, MCP for hosted agents, disconnect and reconnect."""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from app import agent_api, agent_hosts, mcp_server, runner_api
from app.remote import hash_token
from app.repo import MemoryRepo, RemoteJobRepo
from daash_runner import pyz
from tests.integration.runner_helpers import BODY, b64, make_remote_job, new_runner

PUBLIC = "http://daash.example:8001"
FITZ = {"kind": "openclaw", "ref": "main", "name": "Fitz", "available": True,
        "detail": "OpenClaw 2026.10.9 agent 'main'; gateway live on 127.0.0.1:18789"}
HERMES = {"kind": "hermes", "ref": "default", "name": "Hermes", "available": True,
          "detail": "Hermes Agent profile 'default'"}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(runner_api, "LEASE_POLL_SECONDS", 0.05)


@pytest_asyncio.fixture
async def agent_client(pool, bus, settings, monkeypatch):
    monkeypatch.setattr(agent_api, "pool", pool)
    monkeypatch.setattr(agent_api, "bus", bus)
    monkeypatch.setattr(agent_api, "settings", settings)
    monkeypatch.setattr(agent_api, "embedder", agent_api.build_embedder("none", False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_api.app),
                                 base_url="http://agents") as client:
        yield client


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def enroll_runner(api, agent_client, settings, host="m3u512gb", name=""):
    """A new connect code redeemed on the agent port: (runner id, runner token)."""
    settings.AGENT_PUBLIC_URL = PUBLIC
    code = (await api.post("/runner-enrollments")).json()["code"]
    r = await agent_client.post("/runner/enroll", json={"code": code, "host": host, "name": name,
                                                        "version": "0.4.0"})
    assert r.status_code == 201, r.text
    return r.json()["runner_id"], r.json()["token"]


async def hello(client, token, agents, host="m3u512gb"):
    r = await client.post("/runner/hello", headers=bearer(token), json={
        "host": host, "version": "0.4.0", "max_jobs": 2, "harnesses": [], "agents": agents})
    assert r.status_code == 200, r.text
    return r.json()


async def rows(pool):
    return {r["name"]: dict(r) for r in await pool.fetch(
        "SELECT id, name, protocol, kind, status, runner_id, local_ref, url, capabilities, card, "
        "last_error, mcp_token_hash FROM connected_agents")}


async def pinned_job(h, pool, ca_row, task_name="Relay task"):
    """An In-Progress task with a queued `connected` job for a runner-hosted agent, with the
    payload keys the engine uses for agent hosts."""
    job, task = await make_remote_job(h, pool, harness="connected", model="", task_name=task_name,
                                      artifact=f"{task_name.lower().replace(' ', '-')}.md")
    payload = dict(job.payload)
    payload.update({"connected_agent_id": ca_row["id"], "protocol": "runner",
                    "runner_id": ca_row["runner_id"], "relay": agent_hosts.RELAYS[ca_row["kind"]],
                    "local_ref": ca_row["local_ref"]})
    await pool.execute("UPDATE remote_jobs SET payload = $2 WHERE id = $1", job.id, payload)
    return await RemoteJobRepo(pool).get(job.id), task


# -- connect codes (admin) ------------------------------------------------------------------------

async def test_connect_codes_need_the_public_url(api, settings):
    settings.AGENT_PUBLIC_URL = ""
    r = await api.post("/runner-enrollments")
    assert r.status_code == 409 and "DAASH_AGENT_PUBLIC_URL" in r.json()["detail"]


async def test_connect_code_lifecycle(api, agent_client, pool, settings):
    settings.AGENT_PUBLIC_URL = PUBLIC + "/"
    r = await api.post("/runner-enrollments")
    assert r.status_code == 201
    body = r.json()
    code = body["code"]
    assert code.startswith("dre_") and body["status"] == "pending"
    assert body["command"] == (f"curl -fsSLo ~/daash-runner.pyz {PUBLIC}/runner/daash-runner.pyz "
                               f"&& python3 ~/daash-runner.pyz connect {PUBLIC} {code}")
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=59) < expires - datetime.now(timezone.utc) <= timedelta(minutes=60)
    stored = await pool.fetchval("SELECT code_hash FROM runner_enrollments")
    assert stored == agent_hosts.hash_code(code) and stored != code      # only the hash is kept

    listed = (await api.get("/runner-enrollments")).json()["enrollments"]
    assert [e["id"] for e in listed] == [body["id"]] and "code" not in listed[0]
    one = (await api.get(f"/runner-enrollments/{body['id']}")).json()
    assert one["status"] == "pending" and one["runner"] is None and one["agents"] == []

    # unknown and redeemed codes
    assert (await agent_client.post("/runner/enroll", json={"code": "dre_nope"})).status_code == 404
    r = await agent_client.post("/runner/enroll", json={"code": code, "host": "m3u512gb"})
    assert r.status_code == 201
    token = r.json()["token"]
    assert token.startswith("daash_rt_") and r.json()["name"] == "m3u512gb"
    again = await agent_client.post("/runner/enroll", json={"code": code, "host": "m3u512gb"})
    assert again.status_code == 410 and "already used" in again.json()["detail"]
    assert (await pool.fetchval("SELECT token_hash FROM runners")) == hash_token(token)

    assert (await api.get("/runner-enrollments")).json()["enrollments"] == []
    used = (await api.get("/runner-enrollments?all=true")).json()["enrollments"]
    assert used[0]["status"] == "used" and used[0]["runner_id"] == r.json()["runner_id"]
    await hello(agent_client, token, [FITZ])
    one = (await api.get(f"/runner-enrollments/{body['id']}")).json()
    assert one["status"] == "used" and one["runner"]["name"] == "m3u512gb"
    assert one["runner"]["online"] is True
    assert [a["name"] for a in one["agents"]] == ["Fitz"] and one["agents"][0]["relay"] == \
        "openclaw-agent"
    assert (await api.delete(f"/runner-enrollments/{body['id']}")).status_code == 409

    # expired codes, revoked codes, a second machine with the same host name
    code2 = (await api.post("/runner-enrollments")).json()["code"]
    await pool.execute("UPDATE runner_enrollments SET expires_at = now() - interval '1 second' "
                       "WHERE code_hash = $1", agent_hosts.hash_code(code2))
    r = await agent_client.post("/runner/enroll", json={"code": code2})
    assert r.status_code == 410 and "expired" in r.json()["detail"]
    third = (await api.post("/runner-enrollments")).json()
    assert (await api.delete(f"/runner-enrollments/{third['id']}")).status_code == 204
    assert (await agent_client.post("/runner/enroll", json={"code": third["code"]})
            ).status_code == 404
    _, _ = await enroll_runner(api, agent_client, settings, host="m3u512gb")
    names = sorted(r["name"] for r in await pool.fetch("SELECT name FROM runners"))
    assert names == ["m3u512gb", "m3u512gb-2"]


async def test_admin_token_guards_codes_but_not_enroll(api, agent_client, settings):
    settings.API_TOKEN = "admin-secret"
    settings.AGENT_PUBLIC_URL = PUBLIC
    assert (await api.post("/runner-enrollments")).status_code == 401
    code = (await api.post("/runner-enrollments", headers=bearer("admin-secret"))).json()["code"]
    # the agent port has no admin routes; enrolling needs only the code (on either port)
    assert (await agent_client.post("/runner-enrollments")).status_code in (404, 405)
    assert (await agent_client.get("/runner-enrollments")).status_code == 404
    assert (await api.post("/runner/enroll", json={"code": code})).status_code == 201


async def test_zipapp_download(agent_client, settings, tmp_path):
    settings.RUNNER_ZIPAPP_PATH = str(tmp_path / "missing.pyz")
    r = await agent_client.get("/runner/daash-runner.pyz")
    assert r.status_code == 404 and "Rebuild" in r.json()["detail"]
    settings.RUNNER_ZIPAPP_PATH = pyz.build(str(tmp_path / "daash-runner.pyz"))
    r = await agent_client.get("/runner/daash-runner.pyz")
    assert r.status_code == 200 and r.content.startswith(b"#!/usr/bin/env python3\n")
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "__main__.py" in names and "daash_runner/host.py" in names
    assert not any("__pycache__" in n for n in names)


# -- hello with local agents --------------------------------------------------------------------------

async def test_hello_upserts_hosted_agents_and_returns_tokens_once(api, agent_client, pool, settings):
    runner_id, token = await enroll_runner(api, agent_client, settings)
    first = await hello(agent_client, token, [FITZ, HERMES])
    assert set(first["agent_tokens"]) == {"main", "default"}
    assert all(t.startswith("dca_") for t in first["agent_tokens"].values())
    assert [(a["name"], a["status"], a["kind"], a["local_ref"]) for a in first["agents"]] == [
        ("Fitz", "connected", "openclaw", "main"), ("Hermes", "connected", "hermes", "default")]
    got = await rows(pool)
    fitz = got["Fitz"]
    assert fitz["protocol"] == "runner" and fitz["runner_id"] == runner_id and fitz["url"] is None
    assert fitz["capabilities"] == ["edit_files", "run_code"]
    assert fitz["card"]["host"] == "m3u512gb" and fitz["card"]["relay"] == "openclaw-agent"
    assert fitz["mcp_token_hash"] == hash_token(first["agent_tokens"]["main"])

    # tokens come once; later hellos update status and detail
    gw_down = dict(FITZ, available=False, detail="its gateway is not reachable")
    second = await hello(agent_client, token, [gw_down, HERMES])
    assert second["agent_tokens"] == {}
    got = await rows(pool)
    assert got["Fitz"]["status"] == "offline" and "gateway" in got["Fitz"]["last_error"]
    assert got["Fitz"]["mcp_token_hash"] == fitz["mcp_token_hash"]
    # a vanished agent goes offline; it comes back connected with its old id
    third = await hello(agent_client, token, [FITZ])
    assert third["agent_tokens"] == {}
    got = await rows(pool)
    assert got["Hermes"]["status"] == "offline" and got["Hermes"]["last_error"] == \
        agent_hosts.GONE_ERROR
    assert got["Fitz"]["status"] == "connected" and got["Fitz"]["last_error"] is None
    await hello(agent_client, token, [FITZ, HERMES])
    assert (await rows(pool))["Hermes"]["status"] == "connected"
    # asking for a new token (the runner lost or never installed it) replaces the hash
    fresh = await hello(agent_client, token, [dict(FITZ, new_token=True), HERMES])
    assert set(fresh["agent_tokens"]) == {"main"}
    assert (await rows(pool))["Fitz"]["mcp_token_hash"] == hash_token(fresh["agent_tokens"]["main"])
    # a 0.3 runner (no "agents") leaves them alone
    r = await agent_client.post("/runner/hello", headers=bearer(token), json={"host": "m3u512gb"})
    assert "agents" not in r.json() and (await rows(pool))["Fitz"]["status"] == "connected"


async def test_names_clash_kinds_share_an_id_and_renames(api, agent_client, pool, settings):
    from app.repo import ConnectedAgentRepo
    await ConnectedAgentRepo(pool).create("Fitz", "a2a", "http://10.0.0.12:18789")
    _, token = await enroll_runner(api, agent_client, settings)
    same_id = dict(HERMES, ref="main", name="Hermes main")
    body = await hello(agent_client, token, [FITZ, same_id])
    assert [(a["name"], a["local_ref"]) for a in body["agents"]] == [
        ("Fitz (m3u512gb)", "main"), ("Hermes main", "hermes:main")]
    assert set(body["agent_tokens"]) == {"main", "hermes:main"}
    await hello(agent_client, token, [dict(FITZ, name="Fitzwilliam"), same_id])
    assert "Fitzwilliam" in await rows(pool)


async def test_reconnecting_a_machine_adopts_its_agents(api, agent_client, pool, settings):
    _, token1 = await enroll_runner(api, agent_client, settings)
    await hello(agent_client, token1, [FITZ])
    before = (await rows(pool))["Fitz"]
    # the owner disconnects: the runner is disabled, the agents go offline, the token stops working
    r = await agent_client.post("/runner/disconnect", headers=bearer(token1))
    assert r.status_code == 200 and r.json()["agents_offline"] == 1
    off = (await rows(pool))["Fitz"]
    assert off["status"] == "offline" and off["last_error"] == agent_hosts.DISCONNECTED_ERROR
    assert (await agent_client.post("/runner/hello", headers=bearer(token1), json={})
            ).status_code == 401
    assert (await agent_client.post("/runner/disconnect", headers=bearer(token1))).status_code == 401
    # ... and connects again with a new code: same agent row, new runner, new MCP token
    runner2, token2 = await enroll_runner(api, agent_client, settings)
    body = await hello(agent_client, token2, [FITZ])
    after = (await rows(pool))["Fitz"]
    assert after["id"] == before["id"] and after["runner_id"] == runner2
    assert after["status"] == "connected" and set(body["agent_tokens"]) == {"main"}
    assert after["mcp_token_hash"] != before["mcp_token_hash"]
    assert len(await rows(pool)) == 1


async def test_disabling_or_deleting_the_runner(api, agent_client, pool, settings):
    runner_id, token = await enroll_runner(api, agent_client, settings)
    await hello(agent_client, token, [FITZ])
    assert (await api.patch(f"/runners/{runner_id}", json={"enabled": False})).status_code == 200
    got = (await rows(pool))["Fitz"]
    assert got["status"] == "offline" and got["last_error"] == agent_hosts.DISABLED_ERROR
    assert (await api.delete(f"/runners/{runner_id}")).status_code == 204
    assert await rows(pool) == {}


# -- work pinned to the agent host -----------------------------------------------------------------

async def test_pinned_job_leased_only_by_its_runner_and_applied(api, agent_client, h, pool,
                                                                settings):
    runner_id, token = await enroll_runner(api, agent_client, settings)
    tokens = (await hello(agent_client, token, [FITZ, HERMES]))["agent_tokens"]
    fitz = (await rows(pool))["Fitz"]
    job, task = await pinned_job(h, pool, fitz)
    # another runner (even one offering every harness) never gets it
    _, other = await new_runner(api, "other")
    await agent_client.post("/runner/hello", headers=bearer(other), json={
        "harnesses": [{"harness": "fake", "models": []}, {"harness": "openclaw", "models": []}]})
    r = await agent_client.post("/runner/lease", headers=bearer(other),
                                json={"wait_seconds": 0, "free_slots": 1})
    assert r.status_code == 204
    # its own runner gets it, with the relay fields and no job MCP token
    r = await agent_client.post("/runner/lease", headers=bearer(token),
                                json={"wait_seconds": 0, "free_slots": 1})
    assert r.status_code == 200, r.text
    leased = r.json()["job"]
    assert leased["id"] == job.id and leased["harness"] == "connected"
    assert (leased["relay"], leased["local_ref"], leased["runner_id"]) == \
        ("openclaw-agent", "main", runner_id)
    assert "mcp" not in leased and leased["workspace_url"] == f"/runner/jobs/{job.id}/workspace"
    assert await pool.fetchval("SELECT mcp_token_hash FROM remote_jobs WHERE id = $1", job.id) is None

    # the agent uses its own dca_ token: DAASH tools, but no workspace file tools
    dca = tokens["main"]
    listed = await agent_client.post("/mcp", headers=bearer(dca), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert "remember" in names and "write_file" not in names and "read_file" not in names
    call = await agent_client.post("/mcp", headers=bearer(dca), json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "remember", "arguments": {"content": "Fitz was here", "job": job.id}}})
    assert call.json()["result"]["isError"] is False
    assert [m.content for m in await MemoryRepo(pool).list(task.assigned_agent_id)] == \
        ["Fitz was here"]
    hermes_dca = tokens["default"]       # another hosted agent cannot act on Fitz's job
    other_call = await agent_client.post("/mcp", headers=bearer(hermes_dca), json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "remember", "arguments": {"content": "x", "job": job.id}}})
    assert other_call.json()["result"]["isError"] is True

    # heartbeat, workspace and result as for any runner job; the files are staged for the engine
    hb = await agent_client.post(f"/runner/jobs/{job.id}/heartbeat", headers=bearer(token), json={})
    assert hb.json() == {"status": "leased"}
    ws = await agent_client.get(leased["workspace_url"], headers=bearer(token))
    assert ws.status_code == 200
    res = await agent_client.post(f"/runner/jobs/{job.id}/result", headers=bearer(token), json={
        "outcome": "submitted", "summary": "Fitz wrote it",
        "files": [{"path": task.artifact, "content_b64": b64(BODY)}], "deleted": [],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7}, "exit_code": 0})
    assert res.status_code == 200, res.text
    done = await RemoteJobRepo(pool).get(job.id)
    assert done.status == "completed" and done.result["outcome"] == "submitted"
    from app import services
    root = services.workspace_dir(settings, task.swarm_id)
    assert (root / ".daash" / "staging" / job.id / task.artifact).read_text() == BODY


async def test_expired_pinned_lease_is_requeued_for_the_same_runner(api, agent_client, h, pool,
                                                                     settings):
    _, token = await enroll_runner(api, agent_client, settings)
    await hello(agent_client, token, [FITZ])
    job, _ = await pinned_job(h, pool, (await rows(pool))["Fitz"])
    lease = {"wait_seconds": 0, "free_slots": 1}
    assert (await agent_client.post("/runner/lease", headers=bearer(token), json=lease)
            ).status_code == 200
    await pool.execute("UPDATE remote_jobs SET lease_expires_at = now() - interval '1 minute' "
                       "WHERE id = $1", job.id)
    assert await RemoteJobRepo(pool).requeue_expired(datetime.now(timezone.utc)) == 1
    again = await RemoteJobRepo(pool).get(job.id)
    assert again.status == "queued" and again.expiries == 1
    r = await agent_client.post("/runner/lease", headers=bearer(token), json=lease)
    assert r.status_code == 200 and r.json()["job"]["id"] == job.id
    # a paused swarm's pinned job is not leased
    await RemoteJobRepo(pool).cancel(job.id)
    job2, _ = await pinned_job(h, pool, (await rows(pool))["Fitz"], "Second task")
    await h.transition("pause")
    assert (await agent_client.post("/runner/lease", headers=bearer(token), json=lease)
            ).status_code == 204
    await h.transition("resume")
    r = await agent_client.post("/runner/lease", headers=bearer(token), json=lease)
    assert r.json()["job"]["id"] == job2.id


async def test_direct_connected_agents_keep_their_file_tools(pool):
    from app.repo import ConnectedAgentRepo
    token = mcp_server.new_connected_token()
    await ConnectedAgentRepo(pool).create("Jane", "a2a", "http://jane:18789",
                                          mcp_token_hash=hash_token(token))
    from app.agent_tools import Repos
    backend = mcp_server.DaashBackend(Repos.from_pool(pool))
    ident = await backend.identify(token)
    assert ident.file_tools is True
    assert "write_file" in {t["name"] for t in backend.list_tools(ident)}

