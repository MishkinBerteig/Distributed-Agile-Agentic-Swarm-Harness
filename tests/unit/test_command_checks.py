"""Command checks (docs/DESIGN-0.3.md section 12): spec validation, evaluation from runner
results, plan parsing, result cleaning, the runner's command executor, review evidence."""
import sys

import pytest

from app import checks, models, prompts, remote
from app.models import AcceptanceCriterion, CheckKind, CriterionCreate
from daash_runner import checks as runner_checks


def crit(arg="python3 -m unittest", cid="c1"):
    return AcceptanceCriterion(id=cid, task_id="t", ordinal=1, text="tests pass",
                               check_kind="command", check_arg=arg)


# --- validation ------------------------------------------------------------------

def test_command_is_deterministic():
    assert checks.is_deterministic("command") and checks.is_deterministic(CheckKind.COMMAND)


@pytest.mark.parametrize("arg,fragment", [
    (None, "needs a shell command"), ("", "needs a shell command"), ("   ", "needs a shell command"),
    ("x" * 301, "too long"), ("a\nb", "single line"), ("a\x00b", "single line"),
    ("a\rb", "single line")])
def test_command_validation_errors(arg, fragment):
    assert fragment in checks.validate_check_spec("command", None, arg)


def test_command_validation_ok_and_path_unused():
    assert checks.validate_check_spec("command", None, "python3 -m unittest") is None
    assert checks.validate_check_spec("command", "ignored.md", "x" * 300) is None


# --- evaluation ------------------------------------------------------------------

def result(cid="c1", code=0, tail="Ran 3 tests\nOK", timed_out=False):
    return {"id": cid, "exit_code": code, "output_tail": tail, "duration_ms": 5,
            "timed_out": timed_out}


def test_green_on_exit_zero(tmp_path):
    r = checks.run_check(crit(), tmp_path, [result()])
    assert r.passed and r.evidence.startswith("exit 0") and "Ran 3 tests" in r.evidence


def test_red_on_nonzero_with_output(tmp_path):
    r = checks.run_check(crit(), tmp_path, [result(code=1, tail="FAIL: test_x")])
    assert not r.passed and r.evidence.startswith("exit 1") and "FAIL: test_x" in r.evidence


def test_red_on_timeout_even_with_exit_zero(tmp_path):
    r = checks.run_check(crit(), tmp_path, [result(code=0, timed_out=True)])
    assert not r.passed and "timed out" in r.evidence


def test_red_when_no_runner_executed(tmp_path):
    for none in (None, [], [result(cid="other")]):
        r = checks.run_check(crit(), tmp_path, none)
        assert not r.passed and "no runner executed this check" in r.evidence


def test_invalid_stored_command_is_red(tmp_path):
    assert not checks.run_check(crit(arg=""), tmp_path, [result()]).passed


def test_result_matched_by_criterion_id(tmp_path):
    rs = [result(cid="a", code=1), result(cid="b", code=0)]
    assert checks.run_check(crit(cid="b"), tmp_path, rs).passed
    assert not checks.run_check(crit(cid="a"), tmp_path, rs).passed


# --- capability and plan -------------------------------------------------------------

def test_with_command_capability():
    cmd = CriterionCreate(text="t", check_kind=CheckKind.COMMAND, check_arg="true")
    rub = CriterionCreate(text="t")
    assert models.with_command_capability([], [cmd]) == ["run_code"]
    assert models.with_command_capability(["run_code"], [cmd]) == ["run_code"]
    assert models.with_command_capability(["web_research"], [cmd, rub]) == ["run_code",
                                                                            "web_research"]
    assert models.with_command_capability([], [rub]) == []


def test_plan_parses_command_criteria_both_shapes():
    obj = {"tasks": [{"name": "A", "artifact": "a.py", "acceptance_criteria": [
        {"text": "tests pass", "check_kind": "command", "check_arg": "python3 -m unittest"},
        {"text": "tests pass 2", "check": "command", "command": "python3 -m unittest -v"},
        {"text": "no cmd", "check_kind": "command"},
        {"text": "multi", "check_kind": "command", "check_arg": "a\nb"}]}]}
    (a,) = prompts.parse_plan(obj, 3)
    kinds = [c.check_kind.value for c in a.criteria]
    assert kinds[:4] == ["command", "command", "rubric", "rubric"]
    assert a.criteria[0].check_arg == "python3 -m unittest" and a.criteria[0].check_path is None
    assert a.criteria[1].check_arg == "python3 -m unittest -v"
    # a command criterion alone does not replace the artifact file_exists check
    assert kinds[-1] == "file_exists"


def test_plan_prompt_mentions_command_checks():
    p = prompts.plan_prompt("T", [], [], 3, None, "", agent_capabilities=[["edit_files", "run_code"]])
    assert "- command (check_arg" in p and "python3 -m unittest" in p and "network" in p
    q = prompts.plan_prompt("T", [], [], 3, None, "", agent_capabilities=[["edit_files"]])
    assert "python3 -m unittest" not in q


def test_review_prompt_shows_command_result_as_daash_evidence():
    c = crit()
    c.status, c.evidence = "green", "exit 0. Ran 3 tests"
    task = type("T", (), {"id": "t", "name": "n", "description": "", "artifact": "", "criteria": [c]})()
    p = prompts.quality_review_prompt(task, [c], {})
    assert "python3 -m unittest" in p and "not by the agent" in p and "GREEN" in p
    assert "Ran 3 tests" in p


# --- result cleaning ------------------------------------------------------------------

def test_clean_check_results():
    raw = [result(tail="x" * 9000), result(cid="dup"), result(cid="dup", code=5), "junk",
           {"id": "", "exit_code": 0}, {"id": "nocode"}, result(cid="evil")]
    out = remote.clean_check_results(raw, allowed_ids=["c1", "dup", "nocode"])
    assert [r["id"] for r in out] == ["c1", "dup", "nocode"]
    assert len(out[0]["output_tail"]) == 4000 and out[1]["exit_code"] == 0
    assert out[2]["exit_code"] is None and out[2]["timed_out"] is False
    assert remote.clean_check_results("x") == []


def test_validate_and_stored_result_carry_check_results():
    v = remote.validate_result({"outcome": "submitted", "check_results": [result()]})
    assert v.check_results[0]["exit_code"] == 0
    assert remote.stored_result(v)["check_results"] == v.check_results
    assert remote.validate_result({"outcome": "submitted"}).check_results == []


# --- the runner's executor -------------------------------------------------------------

def test_runner_runs_commands_in_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("hi")
    out = runner_checks.run_checks([
        {"id": "a", "command": "cat marker.txt && echo done"},
        {"id": "b", "command": "echo oops >&2; exit 7"},
        {"id": "bad", "command": ""}, {"nope": 1}], str(tmp_path))
    assert [r["id"] for r in out] == ["a", "b"]
    assert out[0]["exit_code"] == 0 and "hidone" in out[0]["output_tail"].replace("\n", "")
    assert out[1]["exit_code"] == 7 and "oops" in out[1]["output_tail"]
    assert not out[0]["timed_out"] and out[0]["duration_ms"] >= 0


def test_runner_times_out_and_clips_output(tmp_path):
    cmd = f"{sys.executable} -c \"print('x' * 20000); import time; time.sleep(30)\""
    (r,) = runner_checks.run_checks([{"id": "t", "command": cmd, "timeout_seconds": 1}],
                                    str(tmp_path), poll=0.05)
    assert r["timed_out"] and r["exit_code"] is None and len(r["output_tail"]) <= 4000
    big = f"{sys.executable} -c \"print('y' * 20000)\""
    (r,) = runner_checks.run_checks([{"id": "t", "command": big}], str(tmp_path))
    assert r["exit_code"] == 0 and len(r["output_tail"]) == 4000


def test_runner_abort_via_tick(tmp_path):
    out = runner_checks.run_checks([{"id": "t", "command": "sleep 30"}], str(tmp_path),
                                   tick=lambda: True, poll=0.05)
    assert out is None
