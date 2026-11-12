"""daash_runner adapters for OpenClaw and Hermes Agent (docs/DESIGN-0.4.md 4.3-4.4): argv, env,
generated config, and parsing of output recorded from the real CLIs (OpenClaw 2026.11.3, Hermes
Agent 0.23.1) against the DAASH gateway."""
import json
from pathlib import Path

from daash_runner import adapters

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "runner"
GW = {"DAASH_GATEWAY_URL": "http://gw:12345/v1", "DAASH_LLM_API_KEY": "gw-secret-123"}
HOSTILE_ENV = {
    "PATH": "/usr/bin", "OPENAI_API_KEY": "real-openai", "OPENCLAW_HOME": "/home/u", "OPENCLAW_STATE_DIR": "/home/u/.openclaw",
    "OPENCLAW_CONFIG_PATH": "/home/u/.openclaw/openclaw.json", "HERMES_HOME": "/home/u/.hermes",
    "OPENROUTER_API_KEY": "or", "DAASH_MCP_TOKEN": "djt_stale", "DAASH_MCP_URL": "http://old/mcp",
}


def spec(**kw):
    d = dict(model="fast", system_prompt="You are Delivery-1.", prompt="Do the task.\nArtifact: a.md",
             state_dir="/w/state/x", artifact="a.md", job_dir="/w/jobs/j1", workdir="/w/jobs/j1/ws",
             timeout_seconds=1800)
    d.update(kw)
    return adapters.JobSpec(**d)


MCP = dict(mcp_url="http://coord:8000/mcp", mcp_token="djt_abc")


def arg_after(argv, flag):
    return argv[argv.index(flag) + 1]


def read(name):
    return (FIX / name).read_text()


# ------------------------------------------------------------------ OpenClaw

def oc(**kw):
    h = {"command": "openclaw", "models": ["instant", "fast"], "args": [], "env": dict(GW)}
    h.update(kw)
    return h


def test_openclaw_invocation():
    inv = adapters.build("openclaw", oc(), spec(), HOSTILE_ENV)
    root = "/w/jobs/j1/cli/openclaw"
    assert inv.argv[:3] == ["openclaw", "agent", "exec"]
    assert arg_after(inv.argv, "--config") == f"{root}/openclaw.json"
    assert arg_after(inv.argv, "--state-dir") == f"{root}/state"
    assert arg_after(inv.argv, "--message-file") == "-"
    assert arg_after(inv.argv, "--cwd") == "/w/jobs/j1/ws"
    assert "--json" in inv.argv
    assert arg_after(inv.argv, "--model") == "daash/fast"
    # exec stops itself (exit 2, JSON envelope) before the runner's own timeout kills it
    assert arg_after(inv.argv, "--timeout") == str(1800 - adapters.OPENCLAW_TIMEOUT_MARGIN)
    # private home/state/config: the operator's own OpenClaw is never used
    assert inv.env["OPENCLAW_HOME"] == f"{root}/home" and inv.env["OPENCLAW_STATE_DIR"] == f"{root}/state"
    assert inv.env["OPENCLAW_CONFIG_PATH"] == f"{root}/openclaw.json"
    assert "OPENAI_API_KEY" not in inv.env and "OPENROUTER_API_KEY" not in inv.env
    assert inv.dirs == [f"{root}/home", f"{root}/state"]
    # no system-prompt flag: the system prompt leads the message
    assert inv.stdin.startswith("You are Delivery-1.") and inv.stdin.endswith("Do the task.\nArtifact: a.md")


def test_openclaw_config_points_at_the_gateway_only():
    inv = adapters.build("openclaw", oc(), spec(), HOSTILE_ENV)
    cfg = json.loads(inv.files["/w/jobs/j1/cli/openclaw/openclaw.json"])
    assert cfg["models"]["mode"] == "replace"
    prov = cfg["models"]["providers"]["daash"]
    assert prov["baseUrl"] == "http://gw:12345/v1" and prov["api"] == "openai-completions"
    assert prov["apiKey"] == "${DAASH_LLM_API_KEY}"          # substituted by OpenClaw; never written out
    assert [m["id"] for m in prov["models"]] == ["fast", "instant"]
    assert all(m["compat"]["supportsUsageInStreaming"] for m in prov["models"])
    assert cfg["agents"]["defaults"]["model"]["primary"] == "daash/fast"
    assert "mcp" not in cfg and "DAASH_MCP_TOKEN" not in inv.env and "gw-secret-123" not in json.dumps(cfg)


def test_openclaw_mcp_server():
    inv = adapters.build("openclaw", oc(), spec(**MCP), HOSTILE_ENV)
    cfg = json.loads(inv.files["/w/jobs/j1/cli/openclaw/openclaw.json"])
    assert cfg["mcp"]["servers"]["daash"] == {
        "url": "http://coord:8000/mcp", "transport": "streamable-http",
        "headers": {"Authorization": "Bearer ${DAASH_MCP_TOKEN}"}}
    assert inv.env["DAASH_MCP_TOKEN"] == "djt_abc"
    assert "djt_abc" not in json.dumps(cfg)


def test_openclaw_options():
    inv = adapters.build("openclaw", oc(thinking="low", api_key_env="MY_KEY", args=["--code-mode", "direct"]),
                         spec(timeout_seconds=50), {})
    assert arg_after(inv.argv, "--thinking") == "low" and inv.argv[-2:] == ["--code-mode", "direct"]
    assert arg_after(inv.argv, "--timeout") == "60"
    cfg = json.loads(next(iter(inv.files.values())))
    assert cfg["models"]["providers"]["daash"]["apiKey"] == "${MY_KEY}"
    assert adapters.build("openclaw", oc(), spec(system_prompt=""), {}).stdin == "Do the task.\nArtifact: a.md"


def test_parse_openclaw_ok_fixture():
    p = adapters.parse("openclaw", read("openclaw_ok.json"), "", 0)
    assert p.outcome == "submitted" and p.final_text.startswith("Done") and p.error == ""
    # input excludes cache reads: prompt = input + cacheRead + cacheWrite
    assert p.usage == {"prompt_tokens": 9321 + 18336, "completion_tokens": 405, "total_tokens": 9321 + 18336 + 405}


def test_parse_openclaw_auth_error_is_infrastructure():
    p = adapters.parse("openclaw", read("openclaw_auth_error.json"),
                       "reason=auth next=none detail=401 invalid or missing API key", 1)
    assert p.outcome == "error" and "sign in" in p.error


def test_parse_openclaw_unreachable_gateway_is_error():
    p = adapters.parse("openclaw", read("openclaw_unreachable.json"), "", 2)
    assert p.outcome == "error" and "connect" in p.error


def test_parse_openclaw_timeout_is_failed():
    p = adapters.parse("openclaw", read("openclaw_timeout.json"), "", 2)
    assert p.outcome == "failed" and "timed out" in p.error
    assert p.final_text == ""          # the isError payload is not an answer


def test_parse_openclaw_other_cases():
    assert adapters.parse("openclaw", "", "openclaw: command not found", 127).outcome == "error"
    p = adapters.parse("openclaw", "", "", 2)
    assert p.outcome == "failed" and "timed out" in p.error
    env = {"ok": False, "status": "error", "final": "done it", "model": "fast", "provider": "daash",
           "error": {"message": "temp dir cleanup failed", "kind": "cleanup"}}
    assert adapters.parse("openclaw", json.dumps(env, indent=2), "", 1).outcome == "submitted"
    env = {"ok": False, "status": "error", "final": "", "model": "fast", "provider": "daash",
           "error": {"message": "the agent gave up", "kind": "exception"}}
    p = adapters.parse("openclaw", json.dumps(env), "", 1)
    assert p.outcome == "failed" and p.error == "the agent gave up"
    env = {"ok": True, "status": "ok", "final": "", "payloads": [{"text": "answer from payloads"}]}
    assert adapters.parse("openclaw", "noise line\n" + json.dumps(env, indent=2), "", 0).final_text == "answer from payloads"
    env = {"ok": True, "status": "ok", "final": "", "payloads": []}
    assert adapters.parse("openclaw", json.dumps(env), "", 0).outcome == "failed"


# ------------------------------------------------------------------ Hermes Agent

def hm(**kw):
    h = {"command": "hermes", "models": [], "args": [], "env": dict(GW)}
    h.update(kw)
    return h


HOME = "/w/jobs/j1/cli/hermes-home"


def test_hermes_invocation():
    inv = adapters.build("hermes", hm(), spec(), HOSTILE_ENV)
    assert inv.argv[:2] == ["hermes", "chat"]
    assert arg_after(inv.argv, "--query-file") == "-" and arg_after(inv.argv, "--format") == "stream-json"
    assert "--yolo" in inv.argv
    assert arg_after(inv.argv, "--in") == "/w/jobs/j1/ws"
    assert arg_after(inv.argv, "-m") == "fast"
    assert arg_after(inv.argv, "--max-turns") == "90"
    assert arg_after(inv.argv, "--source") == "tool"
    assert inv.stdin == "Do the task.\nArtifact: a.md"
    # a private HERMES_HOME per job; never the operator's ~/.hermes or their keys
    assert inv.env["HERMES_HOME"] == HOME and inv.dirs == [HOME]
    assert "OPENROUTER_API_KEY" not in inv.env and "OPENAI_API_KEY" not in inv.env
    # the gateway the CLI starts for that home is stopped afterwards; the dependency copy deleted
    assert inv.cleanup == [["hermes", "gateway", "stop"]]
    assert inv.remove_after == [f"{HOME}/installs", f"{HOME}/cache"]


def test_hermes_config():
    inv = adapters.build("hermes", hm(max_turns=40), spec(), HOSTILE_ENV)
    cfg = json.loads(inv.files[f"{HOME}/config.yaml"])        # JSON is valid YAML
    assert cfg["model"] == {"default": "fast", "provider": "custom", "base_url": "http://gw:12345/v1",
                            "key_env": "DAASH_LLM_API_KEY", "context_length": 131072}
    assert cfg["terminal"]["backend"] == "local"
    assert cfg["memory"]["memory_enabled"] is False and cfg["memory"]["user_profile_enabled"] is False
    assert cfg["skills"]["creation_nudge_interval"] == 0
    assert set(cfg["agent"]["disabled_toolsets"]) >= {"memory", "skills"}
    assert cfg["agent"]["system_prompt"] == "You are Delivery-1." and cfg["agent"]["max_turns"] == 40
    assert cfg["auth"]["adopt_external_logins"] is False and cfg["updates"]["check"] is False
    assert "mcp_servers" not in cfg and "DAASH_MCP_TOKEN" not in inv.env
    assert arg_after(inv.argv, "--max-turns") == "40"


def test_hermes_mcp_server():
    inv = adapters.build("hermes", hm(), spec(**MCP), HOSTILE_ENV)
    cfg = json.loads(inv.files[f"{HOME}/config.yaml"])
    srv = cfg["mcp_servers"]["daash"]
    assert srv["url"] == "http://coord:8000/mcp"
    assert srv["headers"] == {"Authorization": "Bearer ${DAASH_MCP_TOKEN}"}
    assert inv.env["DAASH_MCP_TOKEN"] == "djt_abc" and "djt_abc" not in inv.files[f"{HOME}/config.yaml"]


def test_hermes_system_prompt_survives_yaml_specials():
    text = 'Line one: "quoted"\n- item\n# not a comment\n\ttab — ünïcode'
    inv = adapters.build("hermes", hm(), spec(system_prompt=text), {})
    assert json.loads(inv.files[f"{HOME}/config.yaml"])["agent"]["system_prompt"] == text


def test_parse_hermes_ok_fixture():
    p = adapters.parse("hermes", read("hermes_ok.jsonl"), "Session: local-x", 0)
    assert p.outcome == "submitted" and p.final_text.startswith("Created") and p.error == ""
    assert p.usage == {"prompt_tokens": 31744, "completion_tokens": 322, "total_tokens": 32066}


def test_parse_hermes_missing_key_is_infrastructure():
    p = adapters.parse("hermes", read("hermes_no_key.jsonl"), "", 1)
    assert p.outcome == "error" and "No API key" in p.error


def test_parse_hermes_exit_3_is_error():
    assert adapters.parse("hermes", "", "", 3).outcome == "error"
    line = json.dumps({"type": "result", "exit_code": 3, "text": "", "tokens": {}})
    p = adapters.parse("hermes", line, "", 3)
    assert p.outcome == "error" and "exit code 3" in p.error


def test_parse_hermes_other_cases():
    assert adapters.parse("hermes", "", "hermes: command not found", 127).outcome == "error"
    assert adapters.parse("hermes", "", "", 1).outcome == "failed"
    failed = json.dumps({"type": "result", "exit_code": 1, "text": "", "tokens": {},
                         "error": "the agent hit its turn budget"})
    p = adapters.parse("hermes", failed, "", 1)
    assert p.outcome == "failed" and "turn budget" in p.error
    # an empty result text falls back to the streamed text records
    lines = [json.dumps({"type": "text", "text": "Did "}), json.dumps({"type": "text", "text": "it."}),
             json.dumps({"type": "result", "exit_code": 0, "text": "", "tokens": {"input": 5, "output": 2}})]
    p = adapters.parse("hermes", "\n".join(lines), "", 0)
    assert p.outcome == "submitted" and p.final_text == "Did it."
    nothing = json.dumps({"type": "result", "exit_code": 0, "text": "", "tokens": {}})
    assert adapters.parse("hermes", nothing, "", 0).outcome == "failed"


def test_no_secret_in_new_fixtures():
    for name in ("openclaw_ok.json", "openclaw_auth_error.json", "openclaw_unreachable.json",
                 "openclaw_timeout.json", "hermes_ok.jsonl", "hermes_no_key.jsonl"):
        text = read(name)
        assert "sk-" not in text and "Bearer" not in text and "djt_" not in text
