"""The runner as an agent host (docs/DESIGN-0.4.md 5.9): the zipapp, local discovery (parsing
output captured from OpenClaw 2026.10.9 and Hermes Agent 0.23.1 in a test agent host), DAASH tools
for the agents, relay adapters, service files, and connect/disconnect against a fake HOME. No real
agent CLI, service manager or coordinator is used here."""
import json
import os
import plistlib
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from daash_runner import agenttools, discovery, host, hostenv, pyz, relay, service
from daash_runner.client import HttpStatus
from daash_runner.discovery import LocalAgent

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "agent_hosts"
URL = "http://daash.example:8001"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


class FakeRun:
    """Stands in for subprocess.run: answers by argv and records every call."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or []      # [(predicate(argv) -> bool, (code, stdout, stderr))]

    def add(self, pred, code=0, out="", err=""):
        self.answers.append((pred, (code, out, err)))
        return self

    def __call__(self, argv, env=None, input=None, **_kw):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "input": input})
        for pred, (code, out, err) in self.answers:
            if pred(argv):
                return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def argvs(self, *head):
        return [c["argv"] for c in self.calls if c["argv"][1:1 + len(head)] == list(head)]


def has(*parts):
    return lambda argv: all(p in argv for p in parts)


def fake_home(tmp_path):
    home = tmp_path / "home"
    (home / ".hermes" / "profiles" / "helper").mkdir(parents=True)
    (home / ".hermes" / "config.yaml").write_text("model:\n  default: fast\n")
    (home / ".hermes" / ".env").write_text("OPENROUTER_API_KEY=keep-me\n")
    (home / ".hermes" / "profiles" / "helper" / "config.yaml").write_text("model: {}\n")
    (home / ".openclaw" / "workspace").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("openclaw", "hermes"):
        p = bindir / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    env = {"HOME": str(home), "PATH": str(bindir), "SHELL": "/nonexistent-shell",
           "DAASH_RUNNER_HOST": "m3u512gb.local", "DAASH_RUNNER_TOKEN": "leak-me-not"}
    return home, bindir, env


def owner_cli(tmp_path, workspace=None):
    """FakeRun answering like the owner's OpenClaw 2026.10.9 and Hermes 0.23.1."""
    ws = workspace or str(tmp_path / "home" / ".openclaw" / "workspace")
    agents = json.loads(fixture("openclaw_agents_list.json"))
    agents[0]["identityName"] = "Fitz"
    agents[0]["workspace"] = ws
    run = FakeRun()
    run.add(has("agents", "list", "--json"), out=json.dumps(agents))
    run.add(has("config", "get", "gateway.port"), out="18789\n")
    run.add(has("--version"), out="OpenClaw 2026.10.9 (8b18b55) — All your chats, one OpenClaw.\n")
    run.add(has("profile", "list"), out=fixture("hermes_profile_list.txt"))
    run.add(has("mcp", "show", "daash"), code=1, out='{"ok": false, "error": {"message": "No MCP"}}')
    run.add(has("config", "get", "mcp_servers.daash.url"), code=1,
            out="Config key not set: mcp_servers.daash.url\n")
    return run


# -- the zipapp -------------------------------------------------------------------------------------

def test_zipapp_builds_and_runs_help(tmp_path):
    out = pyz.build(str(tmp_path / "daash-runner.pyz"))
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o644
    assert open(out, "rb").read(23) == b"#!/usr/bin/env python3\n"
    res = subprocess.run([sys.executable, out, "--help"], capture_output=True, text=True,
                         timeout=60, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "connect" in res.stdout and "disconnect" in res.stdout and "--coordinator" in res.stdout
    res = subprocess.run([sys.executable, out, "connect", "--help"], capture_output=True, text=True,
                         timeout=60, cwd=tmp_path)
    assert "--no-tools" in res.stdout and "--no-service" in res.stdout
    res = subprocess.run([sys.executable, out, "status"], capture_output=True, text=True,
                         timeout=60, cwd=tmp_path,
                         env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")})
    assert res.returncode == 1 and "Not connected" in res.stdout


# -- discovery ----------------------------------------------------------------------------------------

def test_parse_openclaw_agents_from_the_real_cli():
    parsed = discovery.parse_openclaw_agents(fixture("openclaw_agents_list.json"))
    assert parsed == [{"id": "main", "name": "Testy", "workspace": "/home/runner/.openclaw/workspace",
                       "is_default": True, "model": "lmstudio/fast"}]
    noisy = "[plugins] loaded 3\n" + fixture("openclaw_agents_list.json")
    assert discovery.parse_openclaw_agents(noisy)[0]["id"] == "main"
    assert discovery.parse_openclaw_agents("not json") == []
    assert discovery.parse_openclaw_agents('[{"id": ""}, {"name": "x"}, 3]') == []


def test_parse_hermes_profiles_from_the_real_cli():
    assert discovery.parse_hermes_profiles(fixture("hermes_profile_list.txt")) == [
        {"name": "default", "model": "fast", "gateway": "running", "sticky": True},
        {"name": "helper", "model": "fast", "gateway": "running", "sticky": False}]
    assert discovery.parse_hermes_profiles("") == []


def test_gateway_health():
    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    seen = []

    def ok(url, timeout):
        seen.append(url)
        return Resp(b'{"ok":true,"status":"live"}')

    assert discovery.gateway_health(18789, opener=ok) == (True, "gateway live on 127.0.0.1:18789")
    assert seen == ["http://127.0.0.1:18789/health"]

    def down(url, timeout):
        raise ConnectionRefusedError()

    live, why = discovery.gateway_health(18789, opener=down)
    assert not live and "openclaw gateway start" in why


def test_discover_openclaw_and_hermes(tmp_path):
    home, bindir, env = fake_home(tmp_path)
    run = owner_cli(tmp_path)
    agents, notes = discovery.discover({}, str(bindir), env, run=run,
                                       health=lambda port: (True, f"gateway live on {port}"))
    assert notes == []
    assert [(a.kind, a.ref, a.name, a.available) for a in agents] == [
        ("openclaw", "main", "Fitz", True), ("hermes", "default", "Hermes", True),
        ("hermes", "helper", "Hermes helper", True)]
    fitz, herm, helper = agents
    assert fitz.workspace == str(home / ".openclaw" / "workspace") and fitz.is_default
    assert fitz.cli == str(bindir / "openclaw") and "OpenClaw 2026.10.9" in fitz.detail
    assert herm.home == str(home / ".hermes") and helper.home == str(home / ".hermes" / "profiles" / "helper")
    assert fitz.report(tools=True) == {"kind": "openclaw", "ref": "main", "name": "Fitz",
                                       "available": True, "detail": fitz.detail, "tools": True}
    # every CLI ran with the search PATH and without the runner's own secrets
    assert all(c["env"]["PATH"] == str(bindir) and "DAASH_RUNNER_TOKEN" not in c["env"]
               for c in run.calls)
    # gateway down -> reported, but not available; share_agents limits what is offered
    agents, notes = discovery.discover({"share_agents": ["Fitz", "hermes:helper"]}, str(bindir),
                                       env, run=run, health=lambda port: (False, "gateway down"))
    assert [(a.key, a.available) for a in agents] == [("openclaw:main", False),
                                                      ("hermes:helper", True)]
    assert notes == ["not shared (share_agents): hermes:default"]
    # no CLIs at all
    agents, notes = discovery.discover({}, str(tmp_path / "empty"), env, run=run)
    assert agents == [] and len(notes) == 2


def test_hermes_profiles_fall_back_to_folders(tmp_path):
    home, bindir, env = fake_home(tmp_path)
    run = FakeRun().add(has("profile", "list"), code=1, err="boom")
    found, problem = discovery.discover_hermes(str(bindir / "hermes"), env, run=run)
    assert problem == "" and [a.ref for a in found] == ["default", "helper"]


# -- DAASH tools for the agents ------------------------------------------------------------------------

def test_openclaw_tools_install_and_remove():
    mcp = URL + "/mcp"
    run = FakeRun().add(has("mcp", "show"), code=1, out='{"ok": false}')
    ok, msg, retry = agenttools.install_openclaw("/bin/openclaw", {}, mcp, "dca_x", run)
    assert ok and not retry
    assert run.argvs("mcp", "add") == [["/bin/openclaw", "mcp", "add", "daash", "--url", mcp,
                                        "--transport", "streamable-http",
                                        "--header", "Authorization=Bearer dca_x"]]
    # another daash server is never replaced
    other = FakeRun().add(has("mcp", "show"), out='{"url": "http://elsewhere/mcp"}')
    ok, msg, retry = agenttools.install_openclaw("/bin/openclaw", {}, mcp, "dca_x", other)
    assert not ok and not retry and "elsewhere" in msg and other.argvs("mcp", "add") == []
    # ours (an earlier connect): replaced
    ours = FakeRun().add(has("mcp", "show"), out=json.dumps({"url": mcp}))
    assert agenttools.install_openclaw("/bin/openclaw", {}, mcp, "dca_y", ours)[0]
    assert ours.argvs("mcp", "unset") and ours.argvs("mcp", "add")
    # a failing probe may be temporary
    failing = FakeRun().add(has("mcp", "show"), code=1).add(has("mcp", "add"), code=1,
                                                            err="MCP check failed\n  for daash")
    ok, msg, retry = agenttools.install_openclaw("/bin/openclaw", {}, mcp, "dca_x", failing)
    assert not ok and retry and "MCP check failed for daash" in msg
    # removal only touches our server
    assert agenttools.remove_openclaw("/bin/openclaw", {}, mcp, ours)[0]
    assert ours.argvs("mcp", "unset")
    assert "not this DAASH" in agenttools.remove_openclaw("/bin/openclaw", {}, mcp, other)[1]


def test_hermes_tools_install_and_remove(tmp_path):
    home = tmp_path / "hh"
    home.mkdir()
    (home / ".env").write_text("OPENROUTER_API_KEY=keep\nexport OTHER=1")
    (home / ".env").chmod(0o600)
    mcp = URL + "/mcp"
    run = FakeRun().add(has("config", "get"), code=1, out="Config key not set")
    ok, msg, retry = agenttools.install_hermes("/bin/hermes", {"PATH": "/x"}, str(home), mcp,
                                               "dca_h", run)
    assert ok and not retry
    sets = run.argvs("config", "set")
    assert sets == [["/bin/hermes", "config", "set", "mcp_servers.daash.url", mcp],
                    ["/bin/hermes", "config", "set", "mcp_servers.daash.headers.Authorization",
                     "Bearer ${DAASH_MCP_TOKEN}"]]
    assert all(c["env"]["HERMES_HOME"] == str(home) for c in run.calls)
    env_text = (home / ".env").read_text()
    assert env_text == ("OPENROUTER_API_KEY=keep\nexport OTHER=1\n" + agenttools.ENV_MARKER
                        + "\nDAASH_MCP_TOKEN=dca_h\n")
    assert stat.S_IMODE(os.stat(home / ".env").st_mode) == 0o600
    # installing again replaces the line instead of adding a second one
    agenttools.add_env_line(str(home / ".env"), "DAASH_MCP_TOKEN", "dca_2")
    assert (home / ".env").read_text().count("DAASH_MCP_TOKEN") == 1
    # removal
    ours = FakeRun().add(has("config", "get"), out=mcp + "\n")
    ok, msg = agenttools.remove_hermes("/bin/hermes", {}, str(home), mcp, ours)
    assert ok and ours.argvs("config", "unset") == [["/bin/hermes", "config", "unset",
                                                     "mcp_servers.daash"]]
    assert (home / ".env").read_text() == "OPENROUTER_API_KEY=keep\nexport OTHER=1\n"
    # a profile with someone else's daash server is left alone
    theirs = FakeRun().add(has("config", "get"), out="http://elsewhere/mcp\n")
    ok, msg, retry = agenttools.install_hermes("/bin/hermes", {}, str(home), mcp, "t", theirs)
    assert not ok and not retry and theirs.argvs("config", "set") == []
    # a new .env is created owner-only
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    agenttools.add_env_line(str(fresh / ".env"), "DAASH_MCP_TOKEN", "t")
    assert stat.S_IMODE(os.stat(fresh / ".env").st_mode) == 0o600


# -- relays ------------------------------------------------------------------------------------------

def openclaw_agent(tmp_path, cli="/opt/homebrew/bin/openclaw"):
    return LocalAgent(kind="openclaw", ref="main", name="Fitz", cli=cli,
                      workspace=str(tmp_path / "ws"), is_default=True)


def test_job_folder_and_message(tmp_path):
    fitz = openclaw_agent(tmp_path)
    jid = "d6a7066b-5467-4203-a8de-987051b09d0b"
    folder = relay.job_folder("openclaw-agent", fitz, str(tmp_path / "jobs" / jid), jid)
    assert folder == str(tmp_path / "ws" / "daash-jobs" / "d6a7066b")
    herm = LocalAgent(kind="hermes", ref="default", name="Hermes", home="/h")
    assert relay.job_folder("hermes-agent", herm, "/r/jobs/j", "j") == "/r/jobs/j/ws"
    with pytest.raises(ValueError):
        relay.job_folder("openclaw-agent", LocalAgent(kind="openclaw", ref="x", name="x"), "/j", "j")

    job = {"id": jid, "system_prompt": "You are Delivery-2. Values...", "prompt": "Task: write it."}
    text = relay.message(job, folder, tools=True)
    assert text.startswith(f"[DAASH job {jid}] Your working folder for this job is {folder}")
    assert "Work only inside this folder" in text and "MCP server `daash`" in text
    assert f"job={jid}" in text and text.index("You are Delivery-2") < text.index("Task: write it.")
    plain = relay.message(job, folder, tools=False)
    assert "do not call the `daash` MCP server" in plain
    # a prompt that already starts with the system prompt does not get it twice
    once = relay.message({"id": jid, "system_prompt": "SYS", "prompt": "SYS\n\nTask"}, folder, True)
    assert once.count("SYS") == 1


def test_relay_invocations(tmp_path):
    fitz = openclaw_agent(tmp_path)
    inv = relay.build_openclaw_agent(fitz, {"id": "job-1"}, "/r/jobs/job-1/message.md",
                                     {"PATH": "/p", "OPENCLAW_PROFILE": "owner"}, 900)
    assert inv.argv == ["/opt/homebrew/bin/openclaw", "agent", "--agent", "main", "--session-key",
                        "daash:job-1", "--message-file", "/r/jobs/job-1/message.md", "--json",
                        "--timeout", "870"]
    assert inv.stdin is None and inv.env["OPENCLAW_PROFILE"] == "owner"     # the owner's setup
    herm = LocalAgent(kind="hermes", ref="helper", name="Hermes helper", cli="/u/bin/hermes",
                      home="/u/.hermes/profiles/helper")
    inv = relay.build_hermes_agent(herm, "/r/jobs/j/ws", {"PATH": "/p"}, "the message")
    assert inv.argv == ["/u/bin/hermes", "chat", "--query-file", "-", "--format", "stream-json",
                        "--in", "/r/jobs/j/ws", "--source", "tool"]
    assert "--yolo" not in inv.argv and inv.env["HERMES_HOME"] == "/u/.hermes/profiles/helper"
    assert inv.stdin == "the message"


@pytest.mark.parametrize("name,exit_code,outcome,needle", [
    ("openclaw_agent_ok.json", 0, "submitted", ""),
    ("openclaw_agent_timeout.json", 1, "failed", "timed out"),
    ("openclaw_agent_unknown_agent.json", 1, "error", "Unknown agent id"),
    ("openclaw_agent_gateway_down.json", 1, "error", "Gateway not reachable"),
    ("openclaw_agent_llm_unreachable.json", 1, "error", "AI service"),
    ("openclaw_agent_message_file_dash.json", 1, "failed", "Message file not found"),
])
def test_parse_openclaw_gateway_turns(name, exit_code, outcome, needle):
    p = relay.parse("openclaw-agent", fixture(name), "", exit_code)
    assert p.outcome == outcome, p
    assert needle in p.error
    if outcome == "submitted":
        assert p.final_text == "DONE"
        assert p.usage == {"prompt_tokens": 14734 + 41632, "completion_tokens": 493,
                           "total_tokens": 14734 + 41632 + 493}
    assert relay.parse("openclaw-agent", "", "boom", 1).outcome == "failed"


def test_parse_hermes_relay_output():
    p = relay.parse("hermes-agent", fixture("hermes_relay_ok.jsonl"), "", 0)
    assert p.outcome == "submitted" and p.final_text == "DONE"
    assert p.usage["prompt_tokens"] == 51360 and p.usage["completion_tokens"] == 399


def test_apply_file_blocks(tmp_path):
    (tmp_path / "old.md").write_text("old")
    reply = ("Done.\n<<<FILE guides/a.md>>>\n# A\ntext\n<<<END FILE>>>\n"
             "<<<FILE ../escape.md>>>\nx\n<<<END FILE>>>\n<<<FILE /etc/passwd>>>\nx\n<<<END FILE>>>\n"
             "<<<DELETE old.md>>>\n<<<DELETE ../../nope>>>")
    written, deleted = relay.apply_file_blocks(reply, str(tmp_path))
    assert written == ["guides/a.md"] and deleted == ["old.md"]
    assert (tmp_path / "guides" / "a.md").read_text() == "# A\ntext\n"
    assert not (tmp_path.parent / "escape.md").exists() and not (tmp_path / "old.md").exists()


# -- the service -----------------------------------------------------------------------------------

def test_launchd_plist_for_macos(tmp_path):
    env = {"HOME": "/Users/fitzowner"}
    assert service.service_kind("darwin") == "launchd"
    path = service.service_file("launchd", env)
    assert path == "/Users/fitzowner/Library/LaunchAgents/org.daash.runner.plist"
    argv = service.run_command("/opt/homebrew/bin/python3", ["/Users/fitzowner/daash-runner.pyz"])
    data = plistlib.loads(service.launchd_plist(
        argv, "/Users/fitzowner", "/Users/fitzowner/.daash-runner/logs",
        "/opt/homebrew/bin:/usr/bin:/bin"))
    assert data["Label"] == "org.daash.runner"
    assert data["ProgramArguments"] == ["/opt/homebrew/bin/python3",
                                        "/Users/fitzowner/daash-runner.pyz", "run"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin")
    assert data["StandardOutPath"] == "/Users/fitzowner/.daash-runner/logs/service.out.log"
    assert data["StandardErrorPath"] == "/Users/fitzowner/.daash-runner/logs/service.err.log"
    assert hostenv.logs_dir(env) == "/Users/fitzowner/.daash-runner/logs"
    assert hostenv.config_path(env) == "/Users/fitzowner/.daash-runner/config.json"


def test_launchd_install_and_uninstall(tmp_path):
    env = {"HOME": str(tmp_path)}
    run = FakeRun().add(has("bootstrap"), code=5, err="Bootstrap failed: 5: Input/output error")
    ok, path, msgs = service.install("launchd", ["/usr/bin/python3", "/x.pyz", "run"], "/usr/bin",
                                     env, run=run, uid=501)
    assert ok and Path(path).is_file()       # bootstrap failed, the older `load -w` worked
    assert [c["argv"][:3] for c in run.calls] == [["launchctl", "bootout", "gui/501/org.daash.runner"],
                                                  ["launchctl", "bootstrap", "gui/501"],
                                                  ["launchctl", "load", "-w"]]
    assert (tmp_path / ".daash-runner" / "logs").is_dir()
    ok, msgs = service.uninstall("launchd", path, env, run=FakeRun(), uid=501)
    assert ok and not Path(path).exists() and any("removed" in m for m in msgs)


def test_systemd_unit_for_linux(tmp_path):
    env = {"HOME": "/home/o"}
    assert service.service_kind("linux") == "systemd"
    assert service.service_file("systemd", env) == "/home/o/.config/systemd/user/daash-runner.service"
    assert service.service_file("systemd", {"HOME": "/home/o", "XDG_CONFIG_HOME": "/cfg"}) == \
        "/cfg/systemd/user/daash-runner.service"
    unit = service.systemd_unit(["/usr/bin/python3", "/home/o/my files/daash-runner.pyz", "run"],
                                "/home/o", "/home/o/.daash-runner/logs", "/usr/local/bin:/usr/bin")
    assert 'ExecStart=/usr/bin/python3 "/home/o/my files/daash-runner.pyz" run' in unit
    assert "Restart=always" in unit and "WantedBy=default.target" in unit
    assert "Environment=PATH=/usr/local/bin:/usr/bin\n" in unit
    assert 'Environment="PATH=/my bin:/usr/bin"' in service.systemd_unit(["x"], "/h", "/l", "/my bin:/usr/bin")
    assert "StandardOutput=append:/home/o/.daash-runner/logs/service.out.log" in unit
    run = FakeRun()
    ok, path, msgs = service.install("systemd", ["/usr/bin/python3", "/x.pyz", "run"], "/usr/bin",
                                     {"HOME": str(tmp_path)}, run=run)
    assert ok and Path(path).read_text().startswith("[Unit]")
    assert ["systemctl", "--user", "enable", "--now", "daash-runner.service"] in \
        [c["argv"] for c in run.calls]
    assert any("enable-linger" in m for m in msgs)
    assert service.service_kind("win32") is None


# -- hostenv ------------------------------------------------------------------------------------------

def test_search_path_and_cli_env(tmp_path):
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    env = {"HOME": str(tmp_path), "PATH": "/a:/b", "DAASH_RUNNER_TOKEN": "secret", "KEEP": "1"}
    path = hostenv.search_path(env, saved="/b:/c", login="/d:/a")
    parts = path.split(":")
    assert parts[:4] == ["/a", "/b", "/c", "/d"] and str(tmp_path / ".local" / "bin") in parts
    assert len(parts) == len(set(parts))
    cenv = hostenv.cli_env(path, env)
    assert cenv["PATH"] == path and cenv["KEEP"] == "1" and "DAASH_RUNNER_TOKEN" not in cenv
    assert hostenv.expand("~/x", env) == str(tmp_path / "x")
    # a login shell whose profile sets PATH and a variable (as ~/.zprofile would)
    shell = tmp_path / "fakeshell"
    shell.write_text('#!/bin/sh\necho "Welcome!"\nexport PATH=/from/login/shell:/usr/bin:/bin\n'
                     'export FROM_PROFILE=yes\n[ "$1" = "-l" ] && shift\nexec /bin/sh "$@"\n')
    shell.chmod(0o755)
    login = hostenv.login_shell_env({"SHELL": str(shell), "PATH": "/usr/bin:/bin"})
    assert login["PATH"] == "/from/login/shell:/usr/bin:/bin" and login["FROM_PROFILE"] == "yes"
    assert hostenv.login_shell_path({"SHELL": str(shell)}).startswith("/from/login/shell")
    assert hostenv.login_shell_path({"SHELL": str(tmp_path / "missing")}) == ""
    # the agent host runs the owner's CLIs in that environment (its own values win)
    ah = host.AgentHost({"coordinator": URL}, str(tmp_path / "c.json"),
                        {"SHELL": str(shell), "PATH": "/svc", "HOME": str(tmp_path), "OWN": "1"})
    assert ah.env()["FROM_PROFILE"] == "yes" and ah.env()["OWN"] == "1"
    assert ah.path.split(":")[:2] == ["/svc", "/from/login/shell"]


# -- connect / status / disconnect against a fake HOME --------------------------------------------------

class FakeClient:
    instances = []

    def __init__(self, url, token):
        self.url, self.token = url, token
        self.calls = []
        FakeClient.instances.append(self)

    def enroll(self, code, host, name, version, platform):
        self.calls.append(("enroll", code, host, name))
        if code == "dre_used":
            raise HttpStatus(410, json.dumps({"detail": "This connect code was already used."}))
        return {"runner_id": "r-1", "name": name, "token": "daash_rt_" + "x" * 43}

    def hello(self, host, version, max_jobs, harnesses, agents=None):
        self.calls.append(("hello", agents))
        tokens = {"main": "dca_fitz", "default": "dca_hermes", "helper": "dca_helper"}
        return {"runner_id": "r-1", "name": "m3u512gb",
                "agents": [{"name": a["name"], "status": "connected", "kind": a["kind"],
                            "ref": a["ref"], "local_ref": a["ref"]} for a in agents],
                "agent_tokens": {a["ref"]: tokens[a["ref"]] for a in agents}}

    def disconnect(self):
        self.calls.append(("disconnect",))
        return {"status": "disconnected"}

    def health(self):
        return {"status": "ok"}


def connect(env, run, *, out=None, **kw):
    lines = []
    code = host.cmd_connect(URL + "/", kw.pop("code", "dre_good"), environ=env, run=run,
                            health=lambda port: (True, "gateway live"), login_path="",
                            entry=["/home/o/daash-runner.pyz"], out=(out or lines.append),
                            version="0.4.0", client_factory=FakeClient, **kw)
    return code, "\n".join(lines)


def test_connect_and_disconnect(tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    home, bindir, env = fake_home(tmp_path)
    FakeClient.instances = []
    run = owner_cli(tmp_path)
    code, text = connect(env, run)
    assert code == 0, text
    cfg_path = home / ".daash-runner" / "config.json"
    assert stat.S_IMODE(os.stat(cfg_path).st_mode) == 0o600
    cfg = json.loads(cfg_path.read_text())
    assert cfg["coordinator"] == URL and cfg["token"].startswith("daash_rt_")
    assert cfg["name"] == "m3u512gb" and cfg["host"] == "m3u512gb" and cfg["tools"] is True
    assert set(cfg["installed"]) == {"openclaw:main", "hermes:default", "hermes:helper"}
    assert cfg["pending_tokens"] == {} and cfg["openclaw"]["command"] == str(bindir / "openclaw")
    enroll = FakeClient.instances[0]
    assert enroll.token == "" and enroll.calls[0] == ("enroll", "dre_good", "m3u512gb", "m3u512gb")
    reported = FakeClient.instances[1].calls[0][1]
    assert [(a["kind"], a["ref"], a["name"]) for a in reported] == [
        ("openclaw", "main", "Fitz"), ("hermes", "default", "Hermes"),
        ("hermes", "helper", "Hermes helper")]
    # the agents got DAASH tools with their own tokens, through their own CLIs
    assert run.argvs("mcp", "add")[0][-1] == "Authorization=Bearer dca_fitz"
    assert "DAASH_MCP_TOKEN=dca_hermes" in (home / ".hermes" / ".env").read_text()
    assert "DAASH_MCP_TOKEN=dca_helper" in (home / ".hermes" / "profiles" / "helper" / ".env").read_text()
    # the systemd user unit runs the zipapp
    unit = home / ".config" / "systemd" / "user" / "daash-runner.service"
    assert "/home/o/daash-runner.pyz run" in unit.read_text()
    assert cfg["service"]["kind"] == "systemd" and cfg["service"]["path"] == str(unit)
    for needle in ("Agents found on this machine: 3", "Fitz (openclaw 'main'): available",
                   "In DAASH they appear as:", "Changed on this machine:",
                   "python3 ~/daash-runner.pyz disconnect"):
        assert needle in text, text

    # connecting twice is refused
    code, text = connect(env, run)
    assert code == 2 and "already connected" in text

    # status
    lines = []
    assert host.cmd_status(environ=env, run=run, out=lines.append, client_factory=FakeClient,
                           health=lambda port: (True, "live"), login_path="") == 0
    status = "\n".join(lines)
    assert "reachable" in status and "Fitz (openclaw 'main'): available; DAASH tools" in status

    # disconnect: service gone, tools removed through the CLIs, token forgotten, logs kept
    (home / ".daash-runner" / "logs").mkdir(exist_ok=True)
    (home / ".daash-runner" / "logs" / "runner.log").write_text("log\n")
    (home / ".daash-runner" / "jobs" / "j1").mkdir(parents=True)
    undo = owner_cli(tmp_path)
    undo.answers.insert(0, (has("mcp", "show", "daash"), (0, json.dumps({"url": URL + "/mcp"}), "")))
    undo.answers.insert(0, (has("config", "get", "mcp_servers.daash.url"), (0, URL + "/mcp\n", "")))
    lines = []
    assert host.cmd_disconnect(environ=env, run=undo, out=lines.append, client_factory=FakeClient,
                               login_path="") == 0
    assert not cfg_path.exists() and not unit.exists()
    assert not (home / ".daash-runner" / "jobs").exists()
    assert (home / ".daash-runner" / "logs" / "runner.log").exists()
    assert ["systemctl", "--user", "disable", "--now", "daash-runner.service"] in \
        [c["argv"] for c in undo.calls]
    assert undo.argvs("mcp", "unset") and len(undo.argvs("config", "unset")) == 2
    assert "DAASH_MCP_TOKEN" not in (home / ".hermes" / ".env").read_text()
    assert "OPENROUTER_API_KEY=keep-me" in (home / ".hermes" / ".env").read_text()
    assert ("disconnect",) in FakeClient.instances[-1].calls
    # once more: nothing to do
    lines = []
    assert host.cmd_disconnect(environ=env, run=undo, out=lines.append, login_path="") == 0
    assert "not connected" in lines[0]


def test_connect_with_no_tools_no_service_and_bad_codes(tmp_path):
    home, bindir, env = fake_home(tmp_path)
    run = owner_cli(tmp_path)
    code, text = connect(env, run, code="dre_used")
    assert code == 2 and "already used" in text and not (home / ".daash-runner" / "config.json").exists()
    code, text = host.cmd_connect("ftp://x", "c", environ=env, out=lambda s: None), ""
    assert code == 2
    code, text = connect(env, run, no_tools=True, no_service=True)
    assert code == 0 and "--no-tools" in text and "--no-service" in text
    cfg = json.loads((home / ".daash-runner" / "config.json").read_text())
    assert cfg["tools"] is False and cfg["installed"] == {} and cfg["service"] is None
    assert run.argvs("mcp", "add") == [] and run.argvs("config", "set") == []
    assert not (home / ".config").exists()


def test_agent_host_tokens_and_reports(tmp_path):
    home, bindir, env = fake_home(tmp_path)
    run = owner_cli(tmp_path)
    run.add(has("mcp", "add"), code=1, err="probe failed")      # OpenClaw's probe fails once
    cfg = {"coordinator": URL, "token": "t", "tools": True, "path": str(bindir)}
    ah = host.AgentHost(cfg, str(home / ".daash-runner" / "config.json"), env, run=run,
                        health=lambda port: (True, "live"), login_path="")
    report = ah.report()
    assert [r.get("new_token") for r in report] == [True, True, True]
    lines = ah.handle_hello({"agents": [{"local_ref": "main", "kind": "openclaw", "ref": "main"},
                                        {"local_ref": "default", "kind": "hermes", "ref": "default"}],
                             "agent_tokens": {"main": "dca_1", "default": "dca_2"}})
    assert any("will try again" in ln for ln in lines)
    assert set(cfg["installed"]) == {"hermes:default"} and cfg["pending_tokens"] == {"openclaw:main": "dca_1"}
    # the pending token is retried, and no new one is asked for meanwhile
    assert {r["ref"]: r.get("new_token", False) for r in ah.report()} == \
        {"main": False, "default": False, "helper": True}
    run.answers = [a for a in run.answers if not a[0](["x", "mcp", "add"])]
    lines = ah.handle_hello({"agents": [], "agent_tokens": {}})     # the next hello: no new tokens
    assert lines == ["Fitz: added MCP server 'daash' (http://daash.example:8001/mcp) to OpenClaw's config"]
    assert "openclaw:main" in cfg["installed"] and cfg["pending_tokens"] == {}
    assert ah.handle_hello({}) == []
    assert ah.resolve("openclaw-agent", "main").name == "Fitz"
    assert ah.resolve("hermes-agent", "hermes:helper").home.endswith("profiles/helper")
    assert ah.resolve("hermes-agent", "main") is None and ah.resolve("nope", "main") is None
    assert ah.has_tools(ah.resolve("openclaw-agent", "main"))


def test_only_one_openclaw_agent_per_install_gets_tools(tmp_path):
    home, bindir, env = fake_home(tmp_path)
    agents = [{"id": "ops", "identityName": "Ops", "workspace": "/w/ops", "isDefault": False},
              {"id": "main", "identityName": "Fitz", "workspace": "/w/main", "isDefault": True}]
    run = FakeRun().add(has("agents", "list"), out=json.dumps(agents))
    cfg = {"coordinator": URL, "token": "t", "kinds": ["openclaw"]}
    ah = host.AgentHost(cfg, str(home / "c.json"), env, run=run,
                        health=lambda port: (True, "live"), login_path="")
    ah.discover()
    assert ah.tools_agent_keys() == {"openclaw:main"}
    ah.handle_hello({"agent_tokens": {"ops": "dca_ops", "main": "dca_main"},
                     "agents": [{"local_ref": "ops", "kind": "openclaw", "ref": "ops"},
                                {"local_ref": "main", "kind": "openclaw", "ref": "main"}]})
    assert set(cfg["installed"]) == {"openclaw:main"}
    assert [a[-1] for a in run.argvs("mcp", "add")] == ["Authorization=Bearer dca_main"]


# -- a relay job through the runner loop ---------------------------------------------------------------

FAKE_OPENCLAW = r'''#!{python}
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
text = open(args[args.index("--message-file") + 1]).read()
folder = re.search(r"Your working folder for this job is (\S+)", text).group(1)
os.makedirs(os.path.join(folder, "guides"), exist_ok=True)
open(os.path.join(folder, "guides", "basil.md"), "w").write("# Basil\n" + "Basil grows well. " * 30)
os.remove(os.path.join(folder, "old.md"))
print(json.dumps({{"runId": "r", "status": "ok", "summary": "completed",
                  "result": {{"payloads": [{{"text": "Wrote the basil guide."}}],
                             "meta": {{"agentMeta": {{"usage": {{"input": 10, "output": 5}}}}}}}}}}))
'''


class StubHost:
    def __init__(self, agent):
        self.agent = agent
        self.hellos = []

    def report(self):
        return [self.agent.report(tools=True)]

    def handle_hello(self, info):
        self.hellos.append(info)
        return []

    def resolve(self, relay_name, local_ref):
        return self.agent if (relay_name, local_ref) == ("openclaw-agent", "main") else None

    def has_tools(self, agent):
        return True

    def env(self):
        return dict(os.environ, FAKE_LOG=self.log)


def test_relay_job_through_the_runner(tmp_path):
    from tests.unit.test_runner_loop import Stub, job, make_server, stop_runner, wait_for
    from daash_runner.client import Client
    from daash_runner.runner import Runner

    cli = tmp_path / "openclaw"
    cli.write_text(FAKE_OPENCLAW.format(python=sys.executable))
    cli.chmod(0o755)
    agent = LocalAgent(kind="openclaw", ref="main", name="Fitz", cli=str(cli),
                       workspace=str(tmp_path / "agent-ws"))
    stub_host = StubHost(agent)
    stub_host.log = str(tmp_path / "calls.jsonl")
    stub = Stub()
    srv = make_server(stub)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    jid = "abcdef12-0000-0000-0000-000000000000"
    stub.jobs.append(job(jid, harness="connected", model="", relay="openclaw-agent",
                         local_ref="main", connected_agent_id="ca1", protocol="runner",
                         runner_id="r1",
                         checks=[{"id": "c1", "command": "test -s guides/basil.md"}]))
    stub.jobs.append(job("gone-job", harness="connected", relay="openclaw-agent", local_ref="ops"))
    r = Runner(Client(url, "daash_rt_test", timeout=5), {"harnesses": {}}, str(tmp_path / "work"),
               1, host="m3u512gb", environ={}, agent_host=stub_host)
    r.lease_wait, r.poll_interval, r.idle_wait, r.heartbeat_seconds = 0, 0.02, 0.05, 0.05
    t = threading.Thread(target=r.run, daemon=True)
    t.start()
    try:
        assert wait_for(lambda: jid in stub.results and "gone-job" in stub.results)
    finally:
        stop_runner(r, t)
        srv.shutdown()
        srv.server_close()
    res = stub.results[jid]
    assert res["outcome"] == "submitted", res
    assert res["summary"] == "Wrote the basil guide." and res["usage"]["total_tokens"] == 15
    assert [f["path"] for f in res["files"]] == ["guides/basil.md"] and res["deleted"] == ["old.md"]
    assert res["check_results"][0]["exit_code"] == 0
    argv = json.loads(open(stub_host.log).readline())
    assert argv[:5] == ["agent", "--agent", "main", "--session-key", f"daash:{jid}"]
    msg_file = argv[argv.index("--message-file") + 1]
    assert msg_file == str(tmp_path / "work" / "jobs" / jid / "message.md")
    assert stat.S_IMODE(os.stat(msg_file).st_mode) == 0o600
    assert "Work only inside this folder" in open(msg_file).read()
    # the job folder lived inside the agent's workspace and is gone afterwards
    assert str(tmp_path / "agent-ws" / "daash-jobs" / "abcdef12") in open(msg_file).read()
    assert not (tmp_path / "agent-ws" / "daash-jobs").exists()
    # the hello carried the agents; an unknown agent is an infrastructure error
    assert stub.hellos[0]["agents"][0]["ref"] == "main" and stub_host.hellos
    gone = stub.results["gone-job"]
    assert gone["outcome"] == "error" and "no longer on this machine" in gone["error"]
