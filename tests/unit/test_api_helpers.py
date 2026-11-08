from datetime import datetime, timedelta, timezone

import pytest

from app import api_helpers as H
from app.models import CriterionCreate, Limits, TaskStatus

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_list_artifacts_relative_sorted_skips_dirs(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("hello")
    (tmp_path / "b.txt").write_text("xy")
    (tmp_path / "emptydir").mkdir()
    files = H.list_artifacts(tmp_path)
    assert [f["path"] for f in files] == ["b.txt", "docs/a.md"]
    assert files[0]["size"] == 2
    datetime.fromisoformat(files[0]["modified"])


def test_list_artifacts_cap_and_missing_root(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}").write_text("x")
    assert len(H.list_artifacts(tmp_path, cap=3)) == 3
    assert H.list_artifacts(tmp_path / "nope") == []


def test_read_artifact_ok_and_truncated(tmp_path):
    (tmp_path / "a.txt").write_text("abcdef")
    assert H.read_artifact(tmp_path, "a.txt") == {"path": "a.txt", "content": "abcdef",
                                                  "truncated": False}
    r = H.read_artifact(tmp_path, "a.txt", cap=3)
    assert r["content"] == "abc" and r["truncated"] is True


@pytest.mark.parametrize("rel", ["missing.txt", "../x", "/etc/passwd", "sub"])
def test_read_artifact_404_cases(tmp_path, rel):
    (tmp_path / "sub").mkdir()
    with pytest.raises(FileNotFoundError):
        H.read_artifact(tmp_path, rel)


def test_transition_fields_start_sets_started_at_once():
    assert H.transition_fields("start", "ACTIVE", None, NOW) == {
        "lifecycle_state": "ACTIVE", "started_at": NOW}
    earlier = NOW - timedelta(hours=1)
    assert "started_at" not in H.transition_fields("start", "ACTIVE", earlier, NOW)


def test_transition_fields_resume_clears_reason():
    f = H.transition_fields("resume", "ACTIVE", NOW, NOW)
    assert f == {"lifecycle_state": "ACTIVE", "paused_reason": None}
    assert "paused_reason" not in H.transition_fields("pause", "PAUSED", NOW, NOW)


def test_transition_detail_readable():
    d = H.transition_detail("pause", "ACTIVE", "PAUSED")
    assert "paused" in d and "ACTIVE -> PAUSED" in d


def test_limits_block():
    lim = Limits(max_total_tokens=100, max_budget_usd=2.5, max_wall_clock_minutes=60)
    b = H.limits_block(lim, NOW - timedelta(minutes=30), 42, 0.5, NOW)
    assert b == {"tokens_used": 42, "max_total_tokens": 100, "cost_usd": 0.5,
                 "max_budget_usd": 2.5, "elapsed_minutes": 30.0, "max_wall_clock_minutes": 60}


def test_limits_block_not_started():
    assert H.limits_block(Limits(), None, 0, 0.0, NOW)["elapsed_minutes"] == 0.0


def test_criteria_error_mapping():
    ok = [CriterionCreate(text="r"),
          CriterionCreate(text="f", check_kind="file_exists", check_path="a.md")]
    assert H.criteria_error(ok) is None
    bad = ok + [CriterionCreate(text="m", check_kind="min_words", check_path="a.md",
                                check_arg="zero")]
    msg = H.criteria_error(bad)
    assert msg.startswith("Acceptance criterion 3:") and "min_words" in msg
    assert "criterion 1" in H.criteria_error(
        [CriterionCreate(text="x", check_kind="file_exists")])


def test_task_patch_reopen_clears_stamps():
    f = H.task_patch_fields({"status": "Pending"})
    assert f["status"] == "Pending"
    assert f["assigned_agent_id"] is None and f["approved_at"] is None
    assert f["committed_at"] is None


def test_task_patch_reject_keeps_reason_and_enum_value():
    f = H.task_patch_fields({"status": TaskStatus.REJECTED, "rejection_reason": "off topic"})
    assert f == {"status": "Rejected", "rejection_reason": "off topic"}
