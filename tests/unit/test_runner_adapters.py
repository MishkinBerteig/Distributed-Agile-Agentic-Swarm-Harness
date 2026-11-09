"""daash_runner.adapters (argv/env/parsers against recorded real CLI output) and fake_cli."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from daash_runner import adapters, fake_cli

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "runner"
HOSTILE_ENV = {
    "PATH": "/usr/bin", "ANTHROPIC_API_KEY": "real-anthropic", "OPENAI_API_KEY": "real-openai",
    "CODEX_HOME": "/home/u/.codex", "CLAUDE_CONFIG_DIR": "/home/u/.claude", "ANTHROPIC_AUTH_TOKEN": "tok",
    "OPENCLAUDE_CONFIG_DIR": "/home/u/.openclaude", "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
}


def spec(**kw):
    d = dict(model="fast", system_prompt="You are an agent.", prompt="Do the task.\nArtifact: a.md",
             state_dir="/state/x", artifact="a.md")
    d.update(kw)
    return adapters.JobSpec(**d)


def arg_after(argv, flag):
    return argv[argv.index(flag) + 1]


# ------------------------------------------------------------------ argv / env

def test_claude_code_invocation():
    h = {"command": "claude", "args": [], "env": {"ANTHROPIC_BASE_URL": "http://gw:1", "ANTHROPIC_AUTH_TOKEN": "k"}}
    inv = adapters.build("claude-code", h, spec(), HOSTILE_ENV)
    assert inv.argv[:2] == ["claude", "-p"]
    assert arg_after(inv.argv, "--output-format") == "stream-json" and "--verbose" in inv.argv
    assert arg_after(inv.argv, "--model") == "fast"
    assert arg_after(inv.argv, "--permission-mode") == "bypassPermissions"
    assert arg_after(inv.argv, "--append-system-prompt") == "You are an agent."
    assert inv.stdin == spec().prompt
    # private config dir; real-account credentials never inherited
    assert inv.env["CLAUDE_CONFIG_DIR"] == "/state/x"
    assert inv.env["ANTHROPIC_BASE_URL"] == "http://gw:1"
    assert inv.env["ANTHROPIC_AUTH_TOKEN"] == "k"
    assert "ANTHROPIC_API_KEY" not in inv.env and "CLAUDE_CODE_OAUTH_TOKEN" not in inv.env
    assert inv.env["ANTHROPIC_SMALL_FAST_MODEL"] == "instant"
    assert inv.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "instant"


def test_claude_code_never_inherits_user_credentials_without_config():
    inv = adapters.build("claude-code", {"command": "claude", "args": [], "env": {}}, spec(), HOSTILE_ENV)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert var not in inv.env
    assert inv.env["CLAUDE_CONFIG_DIR"] == "/state/x"


def test_openclaude_invocation():
    h = {"command": "openclaude", "args": ["--max-turns", "30"],
         "env": {"OPENAI_BASE_URL": "http://gw/v1", "OPENAI_API_KEY": "k"}}
    inv = adapters.build("openclaude", h, spec(), HOSTILE_ENV)
    assert arg_after(inv.argv, "--permission-mode") == "fullAccess"
    assert arg_after(inv.argv, "--max-turns") == "30"
    assert inv.env["CLAUDE_CODE_USE_OPENAI"] == "1"
    assert inv.env["OPENAI_BASE_URL"] == "http://gw/v1" and inv.env["OPENAI_API_KEY"] == "k"
    assert inv.env["CLAUDE_CONFIG_DIR"] == inv.env["OPENCLAUDE_CONFIG_DIR"] == "/state/x"
    assert "ANTHROPIC_API_KEY" not in inv.env


def test_codex_invocation():
    h = {"command": "codex", "args": [], "env": {"DAASH_GATEWAY_URL": "http://gw/v1", "DAASH_LLM_API_KEY": "k"}}
    inv = adapters.build("codex", h, spec(), HOSTILE_ENV)
    assert inv.argv[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"]
    assert arg_after(inv.argv, "--sandbox") == "workspace-write"
    assert arg_after(inv.argv, "-m") == "fast"
    assert inv.argv[-1] == "-" and inv.stdin == spec().prompt
    cs = [inv.argv[i + 1] for i, a in enumerate(inv.argv) if a == "-c"]
    assert 'model_provider="daash"' in cs
    provider = next(c for c in cs if c.startswith("model_providers.daash="))
    assert 'base_url="http://gw/v1"' in provider and 'wire_api="responses"' in provider
    assert 'env_key="DAASH_LLM_API_KEY"' in provider
    assert 'web_search="disabled"' in cs
    assert 'developer_instructions="You are an agent."' in cs
    assert inv.env["CODEX_HOME"] == "/state/x" and inv.env["DAASH_LLM_API_KEY"] == "k"
    assert "OPENAI_API_KEY" not in inv.env


def test_codex_bypass_sandbox_option():
    h = {"command": "codex", "args": [], "sandbox": "bypass", "env": {}}
    inv = adapters.build("codex", h, spec(), {})
    assert "--dangerously-bypass-approvals-and-sandbox" in inv.argv and "--sandbox" not in inv.argv


def test_codex_system_prompt_is_escaped():
    inv = adapters.build("codex", {"command": "codex", "args": [], "env": {}}, spec(system_prompt='say "hi"\nnow'), {})
    value = next(inv.argv[i + 1] for i, a in enumerate(inv.argv) if a == "-c" and inv.argv[i + 1].startswith("developer_instructions"))
    assert json.loads(value.split("=", 1)[1]) == 'say "hi"\nnow'


def test_fake_invocation_sets_artifact_env():
    inv = adapters.build("fake", {"command": sys.executable, "args": [], "env": {"DAASH_FAKE_MODE": "slow"}}, spec(), {})
    assert inv.argv[1:3] == ["-m", "daash_runner.fake_cli"]
    assert inv.env["DAASH_FAKE_ARTIFACT"] == "a.md" and inv.env["DAASH_FAKE_MODE"] == "slow"


# ------------------------------------------------------------------ parsers on recorded fixtures

def read(name):
    return (FIX / name).read_text()


def test_parse_claude_code_ok_fixture():
    p = adapters.parse("claude-code", read("claude_code_ok.jsonl"), "", 0)
    assert p.outcome == "submitted" and p.final_text == "DONE" and p.error == ""
    assert p.usage == {"prompt_tokens": 11565 + 22592, "completion_tokens": 363, "total_tokens": 11565 + 22592 + 363}
    assert p.cost_usd == pytest.approx(0.0580384)


def test_parse_openclaude_ok_fixture():
    p = adapters.parse("openclaude", read("openclaude_ok.jsonl"), "", 0)
    assert p.outcome == "submitted" and p.final_text == "DONE"
    assert p.usage["prompt_tokens"] == 11583 + 11520 and p.usage["completion_tokens"] == 120


def test_parse_codex_ok_fixture():
    p = adapters.parse("codex", read("codex_ok.jsonl"), "Reading additional input from stdin...", 0)
    assert p.outcome == "submitted" and p.final_text == "DONE"
    assert p.usage == {"prompt_tokens": 17502, "completion_tokens": 138, "total_tokens": 17640}


def test_parse_claude_auth_failure_is_error():
    p = adapters.parse("claude-code", read("claude_code_auth_error.jsonl"), "", 1)
    assert p.outcome == "error" and "401" in p.error


def test_parse_claude_max_turns_is_failed():
    p = adapters.parse("claude-code", read("claude_code_failed.jsonl"), "", 1)
    assert p.outcome == "failed" and "error_max_turns" in p.error
    assert p.usage["prompt_tokens"] == 10000


def test_parse_codex_turn_failed_gateway_rejection_is_error():
    p = adapters.parse("codex", read("codex_turn_failed.jsonl"), "", 1)
    assert p.outcome == "error" and "web_search" in p.error


def test_parse_codex_no_answer_is_failed():
    p = adapters.parse("codex", read("codex_no_answer.jsonl"), "", 0)
    assert p.outcome == "failed" and "final answer" in p.error


def test_parse_no_output_cases():
    assert adapters.parse("claude-code", "", "bash: claude: command not found", 127).outcome == "error"
    p = adapters.parse("codex", "", "something odd", 3)
    assert p.outcome == "failed" and "exit code 3" in p.error
    assert adapters.parse("openclaude", "", "connect ECONNREFUSED 10.0.0.1:12345", 1).outcome == "error"


def test_parse_nonzero_exit_with_success_result_is_failed():
    p = adapters.parse("claude-code", read("claude_code_ok.jsonl"), "", 2)
    assert p.outcome == "failed"


def test_no_secret_in_fixtures():
    for f in FIX.glob("*.jsonl"):
        assert "sk-" not in f.read_text() and "Bearer" not in f.read_text()


# ------------------------------------------------------------------ fake_cli

def run_fake(tmp_path, prompt, mode="ok", env=None, timeout=20):
    e = dict(os.environ, DAASH_FAKE_MODE=mode, PYTHONPATH=str(Path(fake_cli.__file__).resolve().parent.parent))
    e.pop("DAASH_FAKE_ARTIFACT", None)
    e.update(env or {})
    return subprocess.run([sys.executable, "-m", "daash_runner.fake_cli", "--model", "fast", prompt],
                          cwd=tmp_path, env=e, capture_output=True, text=True, timeout=timeout)


def test_fake_ok_writes_artifact_from_artifact_line(tmp_path):
    r = run_fake(tmp_path, "Write the guide.\nArtifact: guides/basil.md\nBe nice.")
    assert r.returncode == 0
    text = (tmp_path / "guides" / "basil.md").read_text()
    assert len(text.split()) >= 120 and text.startswith("# Basil")
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["type"] == "result" and not res["is_error"]
    assert adapters.parse("fake", r.stdout, r.stderr, 0).outcome == "submitted"


def test_fake_artifact_fallbacks():
    assert fake_cli.find_artifact("Deliver notes in `docs/x.md` please") == "docs/x.md"
    assert fake_cli.find_artifact("no path here") == "deliverable.md"
    assert fake_cli.find_artifact("Artifact: a.md", "env.md") == "env.md"


def test_fake_artifact_env(tmp_path):
    r = run_fake(tmp_path, "whatever", env={"DAASH_FAKE_ARTIFACT": "out/z.md"})
    assert r.returncode == 0 and (tmp_path / "out" / "z.md").exists()


def test_fake_fail_and_error_modes(tmp_path):
    r = run_fake(tmp_path, "Artifact: a.md", "fail")
    assert r.returncode == 1 and not (tmp_path / "a.md").exists()
    assert adapters.parse("fake", r.stdout, r.stderr, r.returncode).outcome == "failed"
    r = run_fake(tmp_path, "Artifact: a.md", "error")
    assert adapters.parse("fake", r.stdout, r.stderr, r.returncode).outcome == "error"


def test_fake_slow_mode(tmp_path):
    r = run_fake(tmp_path, "Artifact: a.md", "slow", {"DAASH_FAKE_SLOW_SECONDS": "0.1"})
    assert r.returncode == 0 and (tmp_path / "a.md").exists()


def test_fake_hang_mode_does_not_finish(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        run_fake(tmp_path, "Artifact: a.md", "hang", timeout=0.6)
