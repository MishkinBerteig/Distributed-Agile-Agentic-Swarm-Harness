"""DAASH tools over MCP for runner-launched CLIs (docs/DESIGN-0.4.md 4.1-4.2): the lease's `mcp`
block, the per-CLI MCP config, the runner's file/cleanup handling, and the fake CLI's MCP client
against the stub server in tests/fakes/fake_mcp_server.py."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from daash_runner import adapters, fake_cli
from daash_runner.runner import finish_invocation, mcp_endpoint, prepare_invocation
from tests.fakes import fake_mcp_server
from tests.unit.test_runner_loop import job, start_runner, stop_runner, stub, wait_for  # noqa: F401

STALE = {"PATH": "/usr/bin", "DAASH_MCP_TOKEN": "djt_stale", "DAASH_MCP_URL": "http://old/mcp"}
MCP = dict(mcp_url="http://coord:8000/mcp", mcp_token="djt_abc")


def spec(**kw):
    d = dict(model="fast", system_prompt="sys", prompt="Do it.\nArtifact: a.md", state_dir="/w/state/x",
             artifact="a.md", job_dir="/w/jobs/j1", workdir="/w/jobs/j1/ws", timeout_seconds=600)
    d.update(kw)
    return adapters.JobSpec(**d)


def cfg(harness):
    return {"command": {"claude-code": "claude", "fake": sys.executable}.get(harness, harness),
            "args": [], "env": {"DAASH_GATEWAY_URL": "http://gw/v1"}}


# ------------------------------------------------------------------ the lease

def test_mcp_endpoint_from_lease():
    base = "http://100.64.0.7:8001/"
    assert mcp_endpoint({"mcp": {"path": "/mcp", "token": "djt_x"}}, base) == ("http://100.64.0.7:8001/mcp", "djt_x")
    assert mcp_endpoint({}, base) == ("", "")                                     # older coordinator
    assert mcp_endpoint({"mcp": None}, base) == ("", "")
    assert mcp_endpoint({"mcp": {"path": "/mcp"}}, base) == ("", "")               # no token
    assert mcp_endpoint({"mcp": {"path": "http://evil/mcp", "token": "t"}}, base) == ("", "")
    assert mcp_endpoint({"mcp": {"path": "//evil/mcp", "token": "t"}}, base) == ("", "")


# ------------------------------------------------------------------ per-CLI config

@pytest.mark.parametrize("harness", ["claude-code", "openclaude"])
def test_claude_like_mcp_config(harness):
    inv = adapters.build(harness, cfg(harness), spec(**MCP), STALE)
    path = "/w/jobs/j1/cli/mcp.json"
    i = inv.argv.index("--mcp-config")
    assert inv.argv[i + 1] == path
    # variadic options sit before --model so they cannot swallow anything
    assert inv.argv[i + 2:i + 5] == ["--strict-mcp-config", "--allowedTools", "mcp__daash"]
    assert inv.argv.index("--model") > i
    assert json.loads(inv.files[path]) == {"mcpServers": {"daash": {
        "type": "http", "url": "http://coord:8000/mcp", "headers": {"Authorization": "Bearer ${DAASH_MCP_TOKEN}"}}}}
    assert inv.env["DAASH_MCP_TOKEN"] == "djt_abc" and "djt_abc" not in inv.files[path]


@pytest.mark.parametrize("harness", ["claude-code", "openclaude", "codex", "openclaw", "hermes", "fake"])
def test_no_mcp_without_lease_block_and_no_stale_token(harness):
    inv = adapters.build(harness, cfg(harness), spec(), STALE)
    assert "DAASH_MCP_TOKEN" not in inv.env and "DAASH_MCP_URL" not in inv.env
    assert "--mcp-config" not in inv.argv and not any("mcp_servers" in a for a in inv.argv)
    assert not any("mcp" in json.loads(c) or "mcp_servers" in json.loads(c) for c in inv.files.values())


def test_codex_mcp_config():
    inv = adapters.build("codex", cfg("codex"), spec(**MCP), STALE)
    cs = [inv.argv[i + 1] for i, a in enumerate(inv.argv) if a == "-c"]
    assert 'mcp_servers.daash.url="http://coord:8000/mcp"' in cs
    assert 'mcp_servers.daash.bearer_token_env_var="DAASH_MCP_TOKEN"' in cs
    # `codex exec` cannot ask for approval: without this a sandboxed run refuses the call
    assert 'mcp_servers.daash.default_tools_approval_mode="approve"' in cs
    assert inv.env["DAASH_MCP_TOKEN"] == "djt_abc" and inv.argv[-1] == "-"
    assert not any("djt_abc" in a for a in inv.argv)


def test_fake_mcp_env():
    inv = adapters.build("fake", cfg("fake"), spec(**MCP), STALE)
    assert inv.env["DAASH_MCP_URL"] == "http://coord:8000/mcp" and inv.env["DAASH_MCP_TOKEN"] == "djt_abc"


# ------------------------------------------------------------------ runner file handling

def test_prepare_and_finish_invocation(tmp_path):
    marker = tmp_path / "cleanup-ran"
    gone_dir, gone_file = tmp_path / "home" / "installs", tmp_path / "home" / "x.tmp"
    inv = adapters.Invocation(
        argv=["true"], env={"PATH": os.environ.get("PATH", "/usr/bin"), "MARK": str(marker)},
        files={str(tmp_path / "cli" / "sub" / "mcp.json"): '{"a": 1}'},
        dirs=[str(tmp_path / "state")],
        cleanup=[[sys.executable, "-c", "import os; open(os.environ['MARK'], 'w').write('ok')"],
                 ["definitely-not-a-command-xyz"]],
        remove_after=[str(gone_dir), str(gone_file), str(tmp_path / "missing")])
    prepare_invocation(inv)
    f = tmp_path / "cli" / "sub" / "mcp.json"
    assert f.read_text() == '{"a": 1}' and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert (tmp_path / "state").is_dir()
    gone_dir.mkdir(parents=True)
    (gone_dir / "big").write_text("x")
    gone_file.write_text("y")
    finish_invocation(inv, str(tmp_path))         # a failing cleanup command must not raise
    assert marker.read_text() == "ok"
    assert not gone_dir.exists() and not gone_file.exists() and f.exists()


# ------------------------------------------------------------------ the fake CLI's MCP client

@pytest.fixture
def mcp_server():
    server, state = fake_mcp_server.start(token="djt_good")
    state.url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
    yield state
    server.shutdown()
    server.server_close()


def run_fake(tmp_path, env):
    e = dict(os.environ, PYTHONPATH=str(Path(fake_cli.__file__).resolve().parent.parent))
    for k in ("DAASH_FAKE_ARTIFACT", "DAASH_MCP_URL", "DAASH_MCP_TOKEN", "DAASH_FAKE_MCP", "DAASH_FAKE_MCP_STRICT"):
        e.pop(k, None)
    e.update(env)
    return subprocess.run([sys.executable, "-m", "daash_runner.fake_cli", "--model", "fast", "Artifact: n.md"],
                          cwd=tmp_path, env=e, capture_output=True, text=True, timeout=30)


def test_fake_cli_calls_remember_and_send_message(tmp_path, mcp_server):
    r = run_fake(tmp_path, {"DAASH_FAKE_MCP": "all", "DAASH_MCP_URL": mcp_server.url,
                            "DAASH_MCP_TOKEN": "djt_good", "DAASH_FAKE_MCP_TO": "Delivery-2"})
    assert r.returncode == 0, r.stderr
    assert [c["name"] for c in mcp_server.calls] == ["remember", "send_message"]
    assert mcp_server.calls[1]["arguments"]["to"] == "Delivery-2"
    methods = [q.get("method") for q in mcp_server.requests]
    assert methods[:2] == ["initialize", "notifications/initialized"]
    p = adapters.parse("fake", r.stdout, r.stderr, 0)
    assert p.outcome == "submitted" and "DAASH remember: ok" in p.final_text and "send_message: ok" in p.final_text
    assert (tmp_path / "n.md").exists()


def test_fake_cli_mcp_only_remember(tmp_path, mcp_server):
    r = run_fake(tmp_path, {"DAASH_FAKE_MCP": "remember", "DAASH_MCP_URL": mcp_server.url, "DAASH_MCP_TOKEN": "djt_good"})
    assert r.returncode == 0 and [c["name"] for c in mcp_server.calls] == ["remember"]


def test_fake_cli_strict_mode_fails_on_a_bad_token(tmp_path, mcp_server):
    r = run_fake(tmp_path, {"DAASH_FAKE_MCP": "all", "DAASH_FAKE_MCP_STRICT": "1",
                            "DAASH_MCP_URL": mcp_server.url, "DAASH_MCP_TOKEN": "djt_wrong"})
    assert r.returncode == 1 and mcp_server.calls == []
    p = adapters.parse("fake", r.stdout, r.stderr, 1)
    assert p.outcome == "error" and "401" in p.error         # an auth failure is infrastructure
    assert not (tmp_path / "n.md").exists()


def test_fake_cli_without_mcp_server(tmp_path):
    r = run_fake(tmp_path, {"DAASH_FAKE_MCP": "all"})        # no URL/token in this job
    p = adapters.parse("fake", r.stdout, r.stderr, r.returncode)
    assert p.outcome == "submitted" and "no MCP server" in p.final_text
    r = run_fake(tmp_path, {"DAASH_FAKE_MCP": "all", "DAASH_FAKE_MCP_STRICT": "1"})
    assert r.returncode == 1


def test_fake_mcp_server_protocol(mcp_server):
    c = fake_cli.MCPClient(mcp_server.url, "djt_good")
    info = c.initialize()
    assert info["protocolVersion"] == "2025-06-18" and info["serverInfo"]["name"] == "daash"
    assert {t["name"] for t in c.request("tools/list")["tools"]} >= {"remember", "send_message"}
    assert c.call_tool("remember", {"content": "x"}) == (True, "Remembered (memory #1).")
    assert c.call_tool("nope", {})[0] is False
    with pytest.raises(fake_cli.MCPError):
        c.request("bogus/method")
    with pytest.raises(fake_cli.MCPError, match="401"):
        fake_cli.MCPClient(mcp_server.url, "bad").initialize()


# ------------------------------------------------------------------ runner end to end

def test_runner_gives_the_job_its_mcp_server(stub, tmp_path):  # noqa: F811
    stub.mcp = fake_mcp_server.FakeMCP(token="djt_job1")
    stub.jobs.append(job(mcp={"path": "/mcp", "token": "djt_job1"}))
    r, t = start_runner(stub, tmp_path, extra_env={"DAASH_FAKE_MCP": "all", "DAASH_FAKE_MCP_STRICT": "1"})
    try:
        assert wait_for(lambda: "j1" in stub.results)
    finally:
        stop_runner(r, t)
    res = stub.results["j1"]
    assert res["outcome"] == "submitted", res
    assert [c["name"] for c in stub.mcp.calls] == ["remember", "send_message"]
    assert "DAASH remember: ok" in res["summary"]


def test_runner_without_mcp_in_the_lease(stub, tmp_path):  # noqa: F811
    stub.jobs.append(job())
    r, t = start_runner(stub, tmp_path, extra_env={"DAASH_FAKE_MCP": "all"})
    try:
        assert wait_for(lambda: "j1" in stub.results)
    finally:
        stop_runner(r, t)
    assert stub.results["j1"]["outcome"] == "submitted" and "no MCP server" in stub.results["j1"]["summary"]
