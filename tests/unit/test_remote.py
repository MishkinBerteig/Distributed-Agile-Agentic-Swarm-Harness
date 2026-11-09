"""Pure helpers of docs/DESIGN-0.3.md: tokens, lease matching, result validation, tar, staging,
config validation, harness cycling, the remote prompt."""
from __future__ import annotations

import base64
import io
import tarfile
from types import SimpleNamespace

import pytest

from app import prompts, remote, services
from app.jobs import RemoteError, delivery_spec_model, is_infrastructure_error, workspace_listing
from app.models import HarnessSpec, SwarmConfig
from app.routing import validate_config


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


# -- tokens ---------------------------------------------------------------------------------

def test_runner_token_format_and_hash():
    t = remote.new_runner_token()
    assert t.startswith("daash_rt_") and len(t) == len("daash_rt_") + 43
    assert t != remote.new_runner_token()
    h = remote.hash_token(t)
    assert len(h) == 64 and h == remote.hash_token(t) and h != t


def test_bearer_token_parsing():
    assert remote.bearer_token("Bearer abc") == "abc"
    assert remote.bearer_token("bearer  abc ") == "abc"
    assert remote.bearer_token("Basic abc") == ""
    assert remote.bearer_token(None) == "" and remote.bearer_token("") == ""


def test_tokens_match_is_exact():
    assert remote.tokens_match("a", "a") and not remote.tokens_match("a", "b")
    assert not remote.tokens_match("", "x")


# -- capabilities and lease matching ------------------------------------------------------------

def test_normalize_capabilities_drops_unknown_and_native():
    caps = remote.normalize_capabilities([
        {"harness": "codex", "models": ["fast", "", 3]}, {"harness": "daash"},
        {"harness": "nope", "models": []}, "junk", {"harness": "fake", "models": []}])
    assert caps == [{"harness": "codex", "models": ["fast"]}, {"harness": "fake", "models": []}]
    assert remote.normalize_capabilities("x") == []


def test_normalize_capabilities_any_model_wins_and_merges():
    caps = remote.normalize_capabilities([
        {"harness": "codex", "models": ["a"]}, {"harness": "codex", "models": ["b"]},
        {"harness": "fake", "models": ["a"]}, {"harness": "fake", "models": []}])
    assert caps == [{"harness": "codex", "models": ["a", "b"]}, {"harness": "fake", "models": []}]


def test_job_matches():
    caps = {"codex": ["fast"], "fake": []}
    assert remote.job_matches("codex", "fast", caps)
    assert not remote.job_matches("codex", "great", caps)
    assert remote.job_matches("fake", "anything", caps)
    assert not remote.job_matches("openclaude", "fast", caps)


def test_first_match_is_oldest_matching():
    jobs = [{"harness": "codex", "model": "great"}, {"harness": "fake", "model": "x", "n": 2},
            {"harness": "fake", "model": "y", "n": 3}]
    assert remote.first_match(jobs, {"fake": []})["n"] == 2
    assert remote.first_match(jobs, {"claude-code": []}) is None


# -- paths ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["", "  ", "/etc/passwd", "../x", "a/../../x", "a/..", "a\\b",
                                  "a\x00b", ".daash/x", ".daash/staging/j/f", ".git/config",
                                  "sub/.claude/x", "node_modules/p/i.js", ".codex/a", ".openclaude/a",
                                  None, 5, "./"])
def test_bad_paths_rejected(path):
    with pytest.raises(remote.ResultError):
        remote.validate_rel_path(path)


def test_good_paths_normalised():
    assert remote.validate_rel_path("guides/basil.md") == "guides/basil.md"
    assert remote.validate_rel_path("./a//b.txt") == "a/b.txt"
    assert remote.validate_rel_path(".github/x") == ".github/x"


# -- result validation ----------------------------------------------------------------------------

def result(**kw):
    return {"outcome": "submitted", "summary": "done", "files": [], **kw}


def test_validate_result_ok():
    v = remote.validate_result(result(
        files=[{"path": "a/b.md", "content_b64": b64("hello")}], deleted=["old.md", "old.md"],
        usage={"prompt_tokens": 3, "completion_tokens": 4}, cost_usd=0.5, duration_ms=9,
        exit_code=0, transcript_tail="t" * 30000))
    assert v.files == [("a/b.md", b"hello")] and v.deleted == ["old.md"]
    assert v.usage["total_tokens"] == 7 and v.cost_usd == 0.5 and len(v.transcript_tail) == 20000
    stored = remote.stored_result(v)
    assert stored["files"] == ["a/b.md"] and "content_b64" not in str(stored)


@pytest.mark.parametrize("body", [
    "x", {"outcome": "weird"}, result(files="x"), result(files=[1]),
    result(files=[{"path": "../x", "content_b64": ""}]),
    result(files=[{"path": "a", "content_b64": "!!notb64"}]),
    result(files=[{"path": "a"}]),
    result(files=[{"path": "a", "content_b64": ""}, {"path": "./a", "content_b64": ""}]),
    result(deleted=["/abs"]), result(deleted=".daash/x")])
def test_validate_result_rejects(body):
    with pytest.raises(remote.ResultError):
        remote.validate_result(body)


def test_validate_result_limits():
    f = lambda n: {"path": f"f{n}", "content_b64": b64("xxxx")}  # noqa: E731
    with pytest.raises(remote.ResultError, match="too many"):
        remote.validate_result(result(files=[f(1), f(2)]), max_files=1)
    with pytest.raises(remote.ResultError, match="limit"):
        remote.validate_result(result(files=[f(1)]), max_file_bytes=3)
    with pytest.raises(remote.ResultError, match="total"):
        remote.validate_result(result(files=[f(1), f(2)]), max_total_bytes=7)


# -- tar + staging --------------------------------------------------------------------------------

def test_workspace_tar_excludes_daash_and_symlinks(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.md").write_text("x")
    (tmp_path / "top.txt").write_text("t")
    (tmp_path / ".daash" / "staging").mkdir(parents=True)
    (tmp_path / ".daash" / "staging" / "f").write_text("secret")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("o")
    (tmp_path / "link.txt").symlink_to(outside)
    with tarfile.open(fileobj=io.BytesIO(remote.build_workspace_tar(tmp_path)), mode="r:gz") as tar:
        assert sorted(tar.getnames()) == ["a/x.md", "top.txt"]
    assert remote.build_workspace_tar(tmp_path / "missing")   # empty but valid archive


def test_stage_and_apply(tmp_path):
    (tmp_path / "old.md").write_text("old")
    (tmp_path / "keep.md").write_text("keep")
    (tmp_path / "x.md").write_text("before")
    remote.stage_files(tmp_path, "j1", [("x.md", b"after"), ("n/new.md", b"new")])
    written, gone = remote.apply_staged(tmp_path, "j1", ["old.md", "../escape", "missing.md"])
    assert sorted(written) == ["n/new.md", "x.md"] and gone == ["old.md"]
    assert (tmp_path / "x.md").read_text() == "after" and (tmp_path / "keep.md").exists()
    assert not (tmp_path / "old.md").exists()
    assert not remote.staging_dir(tmp_path, "j1").exists()
    assert workspace_listing(tmp_path).count("\n") == 2   # 3 files, .daash hidden


def test_apply_never_deletes_outside_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("v")
    remote.apply_staged(ws, "j", ["../victim.txt"])
    assert victim.exists()


def test_clip_for_listing():
    job = {"payload": {"prompt": "p" * 5000, "system_prompt": "s", "other": 1}}
    out = remote.clip_for_listing(job)
    assert len(out["payload"]["prompt"]) < 2100 and out["payload"]["system_prompt"] == "s"
    assert len(job["payload"]["prompt"]) == 5000


# -- config, cycling, prompt -------------------------------------------------------------------------

def test_config_defaults():
    cfg = SwarmConfig()
    assert cfg.delivery_harnesses == [] and cfg.remote_fallback_minutes == 10
    assert cfg.limits.remote_job_timeout_minutes == 30
    assert validate_config(cfg) == []


def test_config_validation_of_harnesses():
    cfg = SwarmConfig(allowed_harnesses=["daash", "codex"],
                      delivery_harnesses=[HarnessSpec(harness="codex", model="")])
    assert validate_config(cfg) == []
    cfg.delivery_harnesses.append(HarnessSpec(harness="claude-code"))
    assert any("not in allowed_harnesses" in e for e in validate_config(cfg))
    cfg.delivery_harnesses = [HarnessSpec(harness="bogus")]
    assert any("unknown" in e for e in validate_config(cfg))
    assert any("unknown harness 'x'" in e
               for e in validate_config(SwarmConfig(allowed_harnesses=["daash", "x"])))
    assert any("remote_fallback_minutes" in e
               for e in validate_config(SwarmConfig(remote_fallback_minutes=0)))
    assert validate_config(SwarmConfig(remote_fallback_minutes=None)) == []


def test_delivery_harness_cycling():
    cfg = SwarmConfig(allowed_harnesses=["daash", "codex", "fake"], delivery_agents_per_team=4,
                      delivery_harnesses=[HarnessSpec(harness="daash"),
                                          HarnessSpec(harness="codex", model="fast"),
                                          HarnessSpec(harness="fake", model="")])
    got = [services.delivery_spec(cfg, i)[0] for i in range(1, 5)]
    assert got == ["daash", "codex", "fake", "daash"]
    assert [delivery_spec_model(cfg, f"Delivery-{i}") for i in range(1, 5)] == [
        "amazing", "fast", "", "amazing"]
    assert delivery_spec_model(cfg, "weird") == ""
    assert services.delivery_spec(SwarmConfig(), 3) == ("daash", "default")
    assert delivery_spec_model(SwarmConfig(), "Delivery-1") == ""


def test_remote_error_is_infrastructure():
    err = RemoteError("runner crashed")
    assert err.code == "remote" and is_infrastructure_error(err)


def test_remote_work_prompt():
    task = SimpleNamespace(id="t1", name="Basil guide", description="About basil",
                           artifact="guides/basil.md")
    crit = [SimpleNamespace(ordinal=1, text="file exists", check_kind="file_exists",
                            check_path="guides/basil.md", check_arg=None, status="unknown",
                            evidence=None)]
    p = prompts.remote_work_prompt(task, crit, "Add an Overview", "a.md (3 bytes)")
    for needle in ("Basil guide", "guides/basil.md", "file exists", "Add an Overview",
                   "a.md (3 bytes)", "current directory", "Do not ask questions",
                   "short summary"):
        assert needle in p
    assert "submit" not in p
    assert "(empty)" in prompts.remote_work_prompt(task, crit, None, "")
