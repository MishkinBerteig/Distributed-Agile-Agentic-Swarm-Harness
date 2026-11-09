"""daash_runner.config and __main__ argument handling."""
import json
import sys

from daash_runner import __main__ as cli
from daash_runner import config


def test_expand_env_recursively():
    env = {"KEY": "sekret", "H": "host"}
    out = config.expand({"a": "${KEY}", "b": ["x-${H}", {"c": "${MISSING}"}], "n": 3}, env)
    assert out == {"a": "sekret", "b": ["x-host", {"c": ""}], "n": 3}


def test_load_config_expands_and_normalises(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"name": "n", "harnesses": {
        "codex": {"env": {"DAASH_LLM_API_KEY": "${K}"}, "models": ["fast"], "sandbox": "bypass"},
        "claude-code": {"command": "claude"}}}))
    cfg = config.load_config(str(p), {"K": "v"})
    assert cfg["harnesses"]["codex"]["env"] == {"DAASH_LLM_API_KEY": "v"}
    assert cfg["harnesses"]["codex"]["command"] == "codex"
    assert cfg["harnesses"]["codex"]["sandbox"] == "bypass"
    assert cfg["harnesses"]["codex"]["timeout_seconds"] == 1800
    assert cfg["harnesses"]["claude-code"]["models"] == []


def test_available_harnesses_only_found_on_path():
    cfg = config.load_config(None)
    cfg["harnesses"] = {
        "fake": {"command": sys.executable, "models": ["m"], "env": {}, "args": [], "timeout_seconds": 5},
        "codex": {"command": "definitely-not-installed-xyz", "models": [], "env": {}, "args": [], "timeout_seconds": 5},
        "weird": {"command": sys.executable, "models": [], "env": {}, "args": [], "timeout_seconds": 5},
    }
    assert config.available_harnesses(cfg) == [{"harness": "fake", "models": ["m"]}]


def test_cli_args_from_env_and_flags():
    env = {"DAASH_COORDINATOR_URL": "http://c:8000", "DAASH_RUNNER_TOKEN": "t", "DAASH_RUNNER_MAX_JOBS": "3",
           "DAASH_RUNNER_WORKDIR": "/w", "DAASH_RUNNER_CONFIG": "/c.json"}
    a = cli.parse_args([], env)
    assert (a.coordinator, a.token, a.max_jobs, a.work_dir, a.config) == ("http://c:8000", "t", 3, "/w", "/c.json")
    b = cli.parse_args(["--coordinator", "http://x", "--token", "y", "--max-jobs", "5"], env)
    assert (b.coordinator, b.token, b.max_jobs) == ("http://x", "y", 5)


def test_main_requires_coordinator_and_token(capsys, monkeypatch):
    monkeypatch.delenv("DAASH_COORDINATOR_URL", raising=False)
    monkeypatch.delenv("DAASH_RUNNER_TOKEN", raising=False)
    assert cli.main([]) == 2
