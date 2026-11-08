"""Prompt hierarchy, builders and parsers (app/prompts.py)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import models
from app.prompts import (
    ROLE_JOBS, assess_prompt, bootstrap_prompt, classify_prompt, compose_system_prompt,
    extract_quality_json, mission_review_prompt, parse_assessment, parse_bootstrap,
    parse_classification, parse_mission_review, parse_plan, parse_prioritize,
    parse_quality_review, parse_retrospective, parse_standards, plan_prompt, prioritize_prompt,
    quality_review_prompt, retrospective_prompt, standards_prompt, work_prompt,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def crit(ordinal, text, kind="rubric", path=None, arg=None, status="unknown", evidence=None):
    return models.AcceptanceCriterion(id=f"c{ordinal}", task_id="t1", ordinal=ordinal, text=text,
                                      check_kind=kind, check_path=path, check_arg=arg, status=status,
                                      evidence=evidence)


def task(id="t1", name="Write intro", criteria=None):
    return models.Task(id=id, swarm_id="s", team_id="tm", name=name, description="Draft it",
                       artifact="intro.md", created_at=NOW, updated_at=NOW,
                       criteria=criteria if criteria is not None else
                       [crit(1, "intro.md exists", "file_exists", "intro.md"), crit(2, "Clear tone")])


# ------------------------------------------------------------ system prompt

def test_hierarchy_order_and_full_content():
    p = compose_system_prompt(
        vision="V-TEXT", role="mission_delivery", values_principles="VALUES", mission="M-TEXT",
        team_name="Docs", vision_dod="VDOD", team_dor="TDOR", team_dod="TDOD",
        feedback=["fb1", "fb2"], learnings=["L1"], memories=["MEM1"])
    heads = ["# Vision", "# Values and Principles", "# Mission", "# Definition of Done",
             "# Definition of Ready", "# Your Role: Mission Delivery", "# User Feedback",
             "# Learnings", "# Your Memory"]
    idx = [p.index(h) for h in heads]
    assert idx == sorted(idx)
    assert p.index("VDOD") < p.index("TDOD")
    assert p.index("fb1") < p.index("fb2")
    assert p.startswith("# Vision\nV-TEXT")
    assert "majority" in ROLE_JOBS["mission_delivery"]


def test_empty_sections_omitted_and_deterministic():
    kw = dict(vision="V", role="swarm_coordinator")
    p = compose_system_prompt(**kw)
    assert p == compose_system_prompt(**kw)
    assert p.count("# ") == 2 and "# Mission" not in p and "# Learnings" not in p
    assert "# Your Role: Swarm Coordinator" in p


def test_context_lists_capped_at_eight():
    p = compose_system_prompt(vision="V", role="mission_judgement",
                              learnings=[f"learn{i}" for i in range(12)],
                              memories=[f"mem{i}" for i in range(12)])
    assert "learn7" in p and "learn8" not in p and "mem7" in p and "mem8" not in p


def test_role_jobs_cover_all_roles():
    assert set(ROLE_JOBS) == {"swarm_coordinator", "mission_judgement", "quality_judgement",
                              "mission_delivery"}
    assert "accomplished" in ROLE_JOBS["mission_judgement"]
    assert "Definition of Done" in ROLE_JOBS["quality_judgement"]


# ------------------------------------------------------------ bootstrap

def test_bootstrap_ok():
    r = parse_bootstrap({"vision_dod": ["a", "b"], "teams": [
        {"name": "Docs", "mission": "To serve the Vision, write docs"},
        {"name": "docs", "mission": "dup name is dropped silently"},
        {"name": "QA", "mission": "To serve the Vision, test it"}]}, max_teams=2, need_teams=True)
    assert r.vision_dod == "a\nb" and [n for n, _ in r.teams] == ["Docs", "QA"]


def test_bootstrap_without_teams_and_limits():
    assert parse_bootstrap({"vision_dod": "x"}, 3).teams == []
    r = parse_bootstrap({"vision_dod": "x", "teams": [
        {"name": f"T{i}", "mission": "To serve the Vision, do it"} for i in range(5)]}, 2)
    assert len(r.teams) == 2
    assert parse_bootstrap({"vision_dod": "x", "teams": "junk"}, 0).teams == []


@pytest.mark.parametrize("obj,need", [
    ([], False), ({}, False), ({"vision_dod": "x"}, True), ({"vision_dod": "x", "teams": []}, True),
    ({"vision_dod": "x", "teams": [{"name": "A", "mission": ""}]}, True),
    ({"vision_dod": "x", "teams": [{"mission": "To serve the Vision, go"}]}, True),
    ({"vision_dod": "x", "teams": ["A"]}, True)])
def test_bootstrap_invalid(obj, need):
    with pytest.raises(ValueError):
        parse_bootstrap(obj, 3, need_teams=need)


def test_bootstrap_prompt_contract():
    assert '"teams"' in bootstrap_prompt(3, True) and "1 to 3" in bootstrap_prompt(3, True)
    assert '"teams"' not in bootstrap_prompt(3, False)


# ------------------------------------------------------------ standards

def test_standards():
    assert parse_standards({"definition_of_ready": ["a"], "definition_of_done": "b"}) == {
        "definition_of_ready": "a", "definition_of_done": "b"}
    with pytest.raises(ValueError):
        parse_standards({"definition_of_ready": "a"})
    with pytest.raises(ValueError):
        parse_standards({"definition_of_done": "b"})
    with pytest.raises(ValueError):
        parse_standards("x")
    p = standards_prompt("be fast", "be sourced")
    assert "be fast" in p and "be sourced" in p and "definition_of_done" in p


def test_extract_quality_json_kept():
    t = 'Sure:\n```json\n{"definition_of_ready": "r", "definition_of_done": "d"}\n```'
    assert extract_quality_json(t) == {"definition_of_ready": "r", "definition_of_done": "d"}
    with pytest.raises(ValueError):
        extract_quality_json("nothing")


# ------------------------------------------------------------ plan

def test_plan_happy_and_downgrades():
    obj = {"tasks": [
        {"name": "A", "description": "d", "artifact": "a.md", "acceptance_criteria": [
            {"text": "a.md exists", "check_kind": "file_exists", "check_path": "a.md"},
            {"text": "long enough", "check_kind": "min_words", "check_path": "a.md", "check_arg": "100"},
            {"text": "mentions x", "check_kind": "file_contains", "check_path": "a.md", "check_arg": "x+"},
            {"text": "good tone"}]},
        {"name": "B", "artifact": "b.md", "depends_on": [0], "acceptance_criteria": [
            {"text": "bad words", "check_kind": "min_words", "check_path": "b.md", "check_arg": "many"},
            {"text": "bad regex", "check_kind": "file_contains", "check_path": "b.md", "check_arg": "("},
            {"text": "no path", "check_kind": "file_exists"},
            {"text": "escape", "check_kind": "json_valid", "check_path": "../x.json"},
            {"text": "unknown kind", "check_kind": "telepathy"},
            "plain string criterion"]}]}
    a, b = parse_plan(obj, 4)
    assert [c.check_kind.value for c in a.criteria] == ["file_exists", "min_words", "file_contains", "rubric"]
    assert a.criteria[1].check_arg == "100" and a.depends_on == []
    assert all(c.check_kind == models.CheckKind.RUBRIC for c in b.criteria[:6])
    # no deterministic criterion left -> a file_exists on the artifact is added
    assert b.criteria[-1].check_kind == models.CheckKind.FILE_EXISTS and b.criteria[-1].check_path == "b.md"
    assert b.depends_on == [0]


def test_plan_limits_and_bare_list():
    t = {"name": "T", "acceptance_criteria": [{"text": "ok"}]}
    assert len(parse_plan([t, t, t], 2)) == 2
    assert parse_plan([t], 2)[0].artifact == ""


@pytest.mark.parametrize("obj", [
    {}, {"tasks": []}, "x", {"tasks": ["x"]},
    {"tasks": [{"acceptance_criteria": [{"text": "x"}]}]},
    {"tasks": [{"name": "A"}]}, {"tasks": [{"name": "A", "acceptance_criteria": []}]},
    {"tasks": [{"name": "A", "acceptance_criteria": [{"check_kind": "rubric"}]}]},
    {"tasks": [{"name": "A", "acceptance_criteria": ["ok"], "depends_on": [0]}]},
    {"tasks": [{"name": "A", "acceptance_criteria": ["ok"], "depends_on": ["zz"]}]}])
def test_plan_invalid(obj):
    with pytest.raises(ValueError):
        parse_plan(obj, 4)


def test_plan_prompt_content():
    p = plan_prompt("Docs", [task("d1", "Done one")], [task("o1", "Open one")], 3, "focus on X", "a.md  3 bytes")
    for s in ("Docs", "Done one", "Open one", "focus on X", "a.md  3 bytes", "1 to 3", "file_exists",
              "json_valid", "depends_on", "acceptance_criteria"):
        assert s in p


# ------------------------------------------------------------ prioritize

def test_prioritize_ok():
    r = parse_prioritize({"order": ["t2", "t1"], "next_task_id": "t2",
                          "rejections": [{"task_id": "t3", "reason": "off mission"}]}, ["t1", "t2", "t3"])
    assert r.order == ["t2", "t1", "t3"] and r.next_task_id == "t2" and r.rejections == [("t3", "off mission")]


def test_prioritize_defaults_and_null():
    r = parse_prioritize({"order": ["t1", "t2"]}, ["t1", "t2"])
    assert r.next_task_id == "t1"
    r = parse_prioritize({"order": ["t1"], "next_task_id": None,
                          "rejections": [{"task_id": "t1", "reason": "x"}]}, ["t1"])
    assert r.next_task_id is None
    assert parse_prioritize({"order": ["t2", "t1"], "next_task_id": None, "rejections": [
        {"task_id": "t2", "reason": "x"}]}, ["t1", "t2"]).next_task_id == "t1"
    assert parse_prioritize({"order": [1, 2], "next_task_id": 2}, ["1", "2"]).next_task_id == "2"


@pytest.mark.parametrize("obj", [
    [], {"order": "t1"}, {"order": ["zz"]}, {"order": ["t1"], "next_task_id": "zz"},
    {"order": ["t1"], "next_task_id": "t1", "rejections": [{"task_id": "t1", "reason": "x"}]},
    {"order": ["t1"], "rejections": ["t1"]}])
def test_prioritize_invalid(obj):
    with pytest.raises(ValueError) as e:
        parse_prioritize(obj, ["t1", "t2"])
    assert str(e.value)


def test_prioritize_prompt():
    p = prioritize_prompt([task("t1"), task("t2", "Other")], [task("d")], "must cite")
    assert "t1" in p and "Other" in p and "must cite" in p and "next_task_id" in p


# ------------------------------------------------------------ classification

def test_classification():
    assert parse_classification({"complexity": " Moderate "}) == "moderate"
    assert parse_classification("simple") == "simple"
    for bad in ({"complexity": "huge"}, {}, [], None):
        with pytest.raises(ValueError):
            parse_classification(bad)
    assert "Write intro" in classify_prompt(task())


# ------------------------------------------------------------ work prompt

def test_work_prompt():
    t = task()
    prop = models.TeamProposal(id="p1", swarm_id="s", team_id="tm", proposer_agent_id="a",
                               name="NewTeam", mission_statement="Serve the Vision", created_at=NOW)
    p = work_prompt(t, t.criteria, "fix the tone", "intro.md  10 bytes", [prop])
    for s in ("Write intro", "intro.md exists", "fix the tone", "intro.md  10 bytes", "p1", "NewTeam",
              "submit", "self-check"):
        assert s in p
    q = work_prompt(t, t.criteria, None, "", [])
    assert "rejected" not in q and "(empty)" in q and "vote_on_proposal" not in q


# ------------------------------------------------------------ quality review

def review_task():
    return task(criteria=[crit(1, "exists", "file_exists", "intro.md", status="green", evidence="found"),
                          crit(2, "Clear tone"), crit(3, "Accurate")])


def test_quality_review_ok_and_clamp():
    r = parse_quality_review({"passed": "true", "quality_score": "0.97", "feedback": ["a", "b"],
                              "criteria": [{"ordinal": 2, "passed": True, "score": 1.4, "reason": "good"},
                                           {"ordinal": "3", "score": "0.2"},
                                           {"ordinal": 1, "passed": True, "score": 1}]}, [2, 3])
    assert r.passed and r.quality_score == 0.97 and r.feedback == "a\nb"
    assert r.criteria == {2: (True, 1.0, "good"), 3: (False, 0.2, "")}


def test_quality_review_no_rubric_and_derived_score():
    r = parse_quality_review({"passed": False, "quality_score": 1.7}, [])
    assert not r.passed and r.quality_score == 1.0 and r.criteria == {}
    r = parse_quality_review({"passed": True, "criteria": [{"ordinal": 2, "passed": True, "score": 0.8}]}, [2])
    assert r.quality_score == pytest.approx(0.8)


@pytest.mark.parametrize("obj", [
    [], {"quality_score": 1}, {"passed": True, "quality_score": 1},  # missing rubric 2
    {"passed": True, "quality_score": "high", "criteria": []},
    {"passed": "maybe", "quality_score": 1, "criteria": [{"ordinal": 2, "score": 1}]},
    {"passed": True, "quality_score": 1, "criteria": [{"score": 1}]},
    {"passed": True, "quality_score": 1, "criteria": [{"ordinal": 2}]},
    {"passed": True, "quality_score": 1, "criteria": "x"}])
def test_quality_review_invalid(obj):
    with pytest.raises(ValueError):
        parse_quality_review(obj, [2])


def test_quality_review_prompt():
    p = quality_review_prompt(review_task(), review_task().criteria, {"intro.md": "x" * 20000})
    assert "RUBRIC" in p and "GREEN" in p and "found" in p and "--- intro.md ---" in p
    assert "truncated" in p and "2, 3" in p


# ------------------------------------------------------------ mission review / assess / retro

def test_mission_review():
    r = parse_mission_review({"expected_value_met": "yes", "confidence": "99%", "reasoning": "r"})
    assert r.expected_value_met and r.confidence == pytest.approx(0.99) and r.feedback == ""
    assert parse_mission_review({"expected_value_met": False, "confidence": -3,
                                 "feedback": "Add the missing section"}).confidence == 0.0
    for bad in ([], {"confidence": 1}, {"expected_value_met": True},
                {"expected_value_met": True, "confidence": "sure"}, {"expected_value_met": "dunno", "confidence": 1}):
        with pytest.raises(ValueError):
            parse_mission_review(bad)
    assert "Write intro" in mission_review_prompt(task(), "I wrote it", {"intro.md": "hi"})


def test_assessment():
    a = parse_assessment({"status": "Accomplished", "confidence": 0.995, "reasoning": "r", "next_focus": ""})
    assert a.status == "accomplished" and a.confidence == 0.995
    assert parse_assessment({"status": "restart", "confidence": 2, "next_focus": "x"}).confidence == 1.0
    for bad in ([], {"status": "done", "confidence": 1}, {"status": "continue"}):
        with pytest.raises(ValueError):
            parse_assessment(bad)
    team = models.Team(id="tm", swarm_id="s", name="Docs", mission_statement="Write docs", created_at=NOW)
    p = assess_prompt(team, [task("d1", "Done one")], [task("f1", "Failed one")])
    assert "Docs" in p and "Done one" in p and "Failed one" in p and "restart" in p


def test_retrospective():
    r = parse_retrospective({"learnings": [{"kind": "routing", "content": "use great for hard"},
                                           {"kind": "weird", "content": "x"}, "plain"]})
    assert r == [("routing", "use great for hard"), ("pattern", "x"), ("pattern", "plain")]
    for bad in ({}, {"learnings": []}, {"learnings": [{"kind": "pattern"}]}, "x"):
        with pytest.raises(ValueError):
            parse_retrospective(bad)
    assert "history here" in retrospective_prompt("history here")


# -- team proposal vote ---------------------------------------------------------

def test_vote_prompt_content():
    from app.prompts import vote_prompt
    prop = models.TeamProposal(id="p1", swarm_id="s", team_id="tm", proposer_agent_id="a",
                               name="Data Team", mission_statement="Clean the data",
                               reason="too much scope", created_at=NOW)
    text = vote_prompt(prop, "Alpha", [{"id": "t1", "name": "Open one"}],
                       [{"id": "t2", "name": "Done one"}])
    for needle in ("Data Team", "Clean the data", "too much scope", "Alpha", "Open one",
                   "Done one", '"approve"'):
        assert needle in text


def test_parse_vote_happy_and_invalid():
    from app.prompts import parse_vote
    assert parse_vote({"approve": True, "reason": " fits "}) == (True, "fits")
    assert parse_vote({"approve": "no"}) == (False, "")
    for bad in ({}, {"approve": "maybe"}, "nonsense"):
        with pytest.raises(ValueError):
            parse_vote(bad)


def test_mission_review_requires_feedback_when_blocking():
    import pytest as _pytest
    with _pytest.raises(ValueError, match="feedback"):
        parse_mission_review({"expected_value_met": True, "confidence": 0.97, "feedback": ""})
    with _pytest.raises(ValueError, match="feedback"):
        parse_mission_review({"expected_value_met": False, "confidence": 0.99})
    ok = parse_mission_review({"expected_value_met": True, "confidence": 0.97,
                               "feedback": "Add the Blue Jay's call"})
    assert ok.confidence == 0.97 and ok.feedback
    assert parse_mission_review({"expected_value_met": True, "confidence": 0.99}).feedback == ""
