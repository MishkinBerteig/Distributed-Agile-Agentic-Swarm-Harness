"""The runner executes a job's command checks after a submitted CLI run (DESIGN-0.3 section 12)."""
from tests.unit.test_runner_loop import (  # noqa: F401 - stub is a pytest fixture
    job, start_runner, stop_runner, stub, wait_for)


def run_one(stub, tmp_path, mode="ok", **extra):
    stub.jobs.append(job(**extra))
    r, t = start_runner(stub, tmp_path, mode)
    try:
        assert wait_for(lambda: "j1" in stub.results)
    finally:
        stop_runner(r, t)
    return stub.results["j1"]


def test_command_checks_run_after_submitted_and_are_reported(stub, tmp_path):
    res = run_one(stub, tmp_path, checks=[
        {"id": "c1", "command": "test -f guides/basil.md && grep -q Overview guides/basil.md",
         "timeout_seconds": 20},
        {"id": "c2", "command": "echo nope; exit 4", "timeout_seconds": 20}])
    assert res["outcome"] == "submitted"
    by_id = {c["id"]: c for c in res["check_results"]}
    assert by_id["c1"]["exit_code"] == 0 and not by_id["c1"]["timed_out"]
    assert by_id["c2"]["exit_code"] == 4 and "nope" in by_id["c2"]["output_tail"]
    assert set(by_id["c1"]) == {"id", "exit_code", "output_tail", "duration_ms", "timed_out"}


def test_command_check_timeout_is_reported(stub, tmp_path):
    res = run_one(stub, tmp_path, checks=[
        {"id": "c1", "command": "sleep 30", "timeout_seconds": 1}])
    (c,) = res["check_results"]
    assert c["timed_out"] is True and c["exit_code"] is None


def test_command_checks_do_not_run_when_the_agent_failed(stub, tmp_path):
    res = run_one(stub, tmp_path, "fail", checks=[
        {"id": "c1", "command": "true", "timeout_seconds": 20}])
    assert res["outcome"] == "failed" and "check_results" not in res


def test_no_checks_means_no_check_results(stub, tmp_path):
    assert "check_results" not in run_one(stub, tmp_path)


def test_files_made_by_checks_are_not_part_of_the_deliverable(stub, tmp_path):
    res = run_one(stub, tmp_path, checks=[
        {"id": "c1", "command": "echo x > leftover.txt", "timeout_seconds": 20}])
    assert [f["path"] for f in res["files"]] == ["guides/basil.md"]
    assert res["check_results"][0]["exit_code"] == 0
