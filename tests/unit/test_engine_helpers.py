"""Pure engine helpers: cooldown backoff, infra errors, dependency mapping, files, routing stats."""
from datetime import datetime, timezone

from app.engine import Cooldowns, Deferrals, backoff_seconds
from app.jobs import (combine_feedback, dedupe_files, escalate_model, is_infrastructure_error,
                      map_depends, outcomes_from_tasks, read_deliverable_files, review_paths,
                      stats_from_tasks, workspace_listing)
from app.llm import LLMError
from app.models import AcceptanceCriterion, SwarmConfig, Task
from app.prompts import QualityReview

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def mk(id, status="Done", **kw):
    base = dict(id=id, swarm_id="s", team_id="t", name=id, status=status, created_at=T0,
                updated_at=T0)
    base.update(kw)
    return Task(**base)


# -- cooldown -------------------------------------------------------------------

def test_backoff_doubles_and_caps():
    assert [backoff_seconds(n) for n in range(0, 7)] == [0, 30, 60, 120, 240, 300, 300]


def test_deferrals_hold_until_timeout():
    now = [100.0]
    d = Deferrals(clock=lambda: now[0], seconds=300)
    d.hold("prioritize:t", 2)
    assert d.active_keys() == {"prioritize:t"}
    assert d.release_on_progress(2) == []          # same Done count: still held
    now[0] += 299
    assert d.active_keys() == {"prioritize:t"}
    now[0] += 2
    assert d.active_keys() == set()


def test_deferrals_release_when_more_work_is_done():
    d = Deferrals(clock=lambda: 0.0, seconds=300)
    d.hold("a", 2)
    d.hold("b", 3)
    assert d.release_on_progress(3) == ["a"]
    assert d.active_keys() == {"b"}


def test_cooldowns_track_time_and_reset():
    now = [100.0]
    cd = Cooldowns(clock=lambda: now[0])
    assert cd.fail("k") == (30.0, 1)
    assert cd.active_keys() == {"k"}
    now[0] += 31
    assert cd.active_keys() == set()
    assert cd.fail("k") == (60.0, 2)      # failure count survives cooldown expiry
    assert cd.failures("k") == 2
    cd.success("k")
    assert cd.failures("k") == 0 and cd.active_keys() == set()
    assert cd.fail("k") == (30.0, 1)


def test_cooldown_keys_are_independent():
    cd = Cooldowns(clock=lambda: 0.0)
    cd.fail("a")
    cd.fail("a")
    assert cd.fail("b") == (30.0, 1)


# -- infrastructure errors --------------------------------------------------------

def test_infra_error_classification():
    assert is_infrastructure_error(LLMError("x", "connection"))
    assert is_infrastructure_error(LLMError("x", "timeout"))
    assert is_infrastructure_error(LLMError("x", "http", status=503))
    assert is_infrastructure_error(LLMError("x", "http", status=429))
    assert not is_infrastructure_error(LLMError("x", "http", status=400))
    assert not is_infrastructure_error(LLMError("x", "http", status=401))
    assert not is_infrastructure_error(LLMError("x", "parse"))
    assert not is_infrastructure_error(LLMError("x", "protocol"))
    assert not is_infrastructure_error(ValueError("x"))


# -- dependency mapping -------------------------------------------------------------

def test_map_depends():
    assert map_depends([0, 2], ["a", "b", "c"]) == ["a", "c"]
    assert map_depends([], ["a"]) == []
    assert map_depends([0, 0, 5, -1], ["a"]) == ["a"]


# -- deliverable files ----------------------------------------------------------------

def test_dedupe_files_preserves_order():
    assert dedupe_files(["a.md", "b.md", "a.md", "", " c.md "]) == ["a.md", "b.md", "c.md"]


def test_review_paths_collects_deliverable_artifact_and_checks():
    crits = [AcceptanceCriterion(id="1", task_id="t", ordinal=1, text="x",
                                 check_kind="file_exists", check_path="out/a.md"),
             AcceptanceCriterion(id="2", task_id="t", ordinal=2, text="y")]
    t = mk("t", "Review", artifact="a.md", deliverable={"files": ["a.md", "b.md"]}, criteria=crits)
    assert review_paths(t) == ["a.md", "b.md", "out/a.md"]


def test_read_deliverable_files_truncates_and_skips(tmp_path):
    (tmp_path / "big.md").write_text("x" * 50)
    (tmp_path / "small.md").write_text("hello")
    out = read_deliverable_files(tmp_path, ["big.md", "small.md", "missing.md", "../etc/passwd"],
                                 limit=10)
    assert set(out) == {"big.md", "small.md"}
    assert out["small.md"] == "hello"
    assert out["big.md"].startswith("x" * 10) and "truncated" in out["big.md"]


def test_workspace_listing(tmp_path):
    assert workspace_listing(tmp_path / "nope") == ""
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "a.md").write_text("abc")
    assert workspace_listing(tmp_path) == "d/a.md (3 bytes)"


# -- routing stats -----------------------------------------------------------------------

def test_outcomes_and_stats_from_tasks():
    tasks = [mk("a", "Done", complexity="simple", deliverable={"model": "fast"}),
             mk("b", "Failed", complexity="simple", deliverable={"model": "fast"}),
             mk("c", "Done", deliverable={"model": "great"}),
             mk("d", "Done", complexity="simple", deliverable={}),           # no model recorded
             mk("e", "Review", complexity="simple", deliverable={"model": "fast"})]
    assert outcomes_from_tasks(tasks) == [("fast", "simple", True), ("fast", "simple", False),
                                          ("great", None, True)]
    stats = stats_from_tasks(tasks)
    assert stats[("fast", "simple")] == (1, 2)
    assert stats[("great", "moderate")] == (1, 1)


def test_escalate_model():
    cfg = SwarmConfig()
    assert escalate_model("fast", cfg, 0) == "fast"
    assert escalate_model("fast", cfg, 1) == "great"
    assert escalate_model("great", cfg, 5) == "amazing"


# -- feedback --------------------------------------------------------------------------------

def test_combine_feedback_includes_failures():
    qr = QualityReview(passed=False, quality_score=0.4, feedback="Add a conclusion",
                       criteria={1: (False, 0.3, "too short"), 2: (True, 1.0, "fine")})
    text = combine_feedback(["Rubric criterion 1 scored 0.300"], mk("t"), qr, None,
                            ["a.md exists (missing)"])
    assert "Rubric criterion 1" in text and "too short" in text and "fine" not in text
    assert "Add a conclusion" in text and "Check failed" in text
