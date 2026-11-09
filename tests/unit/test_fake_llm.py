"""The scripted fake LLM (tests/fakes/fake_llm.py): job detection, contract-valid answers
that pass the REAL prompts.parse_* functions, a work loop that satisfies the real
deterministic checks, scenarios, and call recording. Pure: no network, DB or Docker."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app import prompts
from app.checks import run_check
from app.llm import LLMClient, LLMError, extract_json
from app.models import AcceptanceCriterion, CheckKind
from app.tools import Tool, ToolRegistry, workspace_tools
from tests.fakes.fake_llm import FakeHTTPError, FakeLLM, detect_job, detect_role, parse_criteria

ROLE = {"coordinator": "swarm_coordinator", "mj": "mission_judgement",
        "qj": "quality_judgement", "delivery": "mission_delivery"}


def system(role="mj", team="Team A"):
    return prompts.compose_system_prompt(
        vision="Write a guide.", role=ROLE.get(role, role), mission="Produce the guide.",
        team_name=team)


def ask(fake: FakeLLM, user: str, role="mj", model="fast", extra=None):
    msgs = [{"role": "system", "content": system(role)}, {"role": "user", "content": user}]
    msgs += extra or []
    return fake.handle({"model": model, "messages": msgs})


def content(resp: dict):
    return resp["choices"][0]["message"]["content"]


def task_obj(i="t1", name="Write intro", artifact="intro.md", criteria=()):
    return SimpleNamespace(id=i, name=name, description="", artifact=artifact,
                           criteria=list(criteria), status="Pending")


def crit(ordinal, text, kind="rubric", path=None, arg=None, status="unknown"):
    return SimpleNamespace(ordinal=ordinal, text=text, check_kind=kind, check_path=path,
                           check_arg=arg, status=status, evidence=None)


def plan_tasks(fake: FakeLLM, team="Team A", cap=4):
    user = prompts.plan_prompt(team, [], [], cap, None, "")
    return prompts.parse_plan(extract_json(content(ask(fake, user, "delivery"))), cap)


# -- detection -----------------------------------------------------------------------

def test_job_detection_covers_every_prompt_builder():
    t = task_obj(criteria=[crit(1, "intro.md exists", "file_exists", "intro.md")])
    cases = {
        "bootstrap": prompts.bootstrap_prompt(3, True),
        "standards": prompts.standards_prompt(),
        "plan": prompts.plan_prompt("A", [], [], 3, None, ""),
        "prioritize": prompts.prioritize_prompt([t], [], None, [SimpleNamespace(
            name="B", status="active", mission_statement="m")]),
        "classify": prompts.classify_prompt(t),
        "work": prompts.work_prompt(t, t.criteria, None, ""),
        "review_quality": prompts.quality_review_prompt(t, t.criteria, {}),
        "review_mission": prompts.mission_review_prompt(t, "s", {}),
        "vote": prompts.vote_prompt(SimpleNamespace(name="N", mission_statement="m", reason=""),
                                    "A", [], []),
        "assess": prompts.assess_prompt(SimpleNamespace(name="A", mission_statement="m"), [], []),
        "retrospective": prompts.retrospective_prompt("history"),
        "review_vision": prompts.vision_review_prompt("V", "dod", [], {}, "", {}),
    }
    for expected, text in cases.items():
        assert detect_job(text) == expected, expected
    assert detect_job("hello") == "unknown"


def test_role_detection_and_recording():
    fake = FakeLLM()
    ask(fake, prompts.standards_prompt(), role="qj", model="great")
    call = fake.calls[-1]
    assert detect_role(system("qj")) == "Quality Judgement"
    assert (call["job"], call["role"], call["model"], call["team"]) == (
        "standards", "Quality Judgement", "great", "Team A")
    assert call["total_tokens"] == call["prompt_tokens"] + call["completion_tokens"] == 150
    assert call["status"] == 200 and call["t_end"] >= call["t"]


# -- contract-valid answers (real parsers) -----------------------------------------------

def test_bootstrap_with_and_without_teams():
    fake = FakeLLM({"teams": 3})
    res = prompts.parse_bootstrap(
        extract_json(content(ask(fake, prompts.bootstrap_prompt(4, True), "coordinator"))), 4, True)
    assert [n for n, _ in res.teams] == ["Team A", "Team B", "Team C"]
    assert all("Vision" in m for _, m in res.teams)
    capped = prompts.parse_bootstrap(
        extract_json(content(ask(fake, prompts.bootstrap_prompt(2, True), "coordinator"))), 2, True)
    assert len(capped.teams) == 2
    none = prompts.parse_bootstrap(
        extract_json(content(ask(fake, prompts.bootstrap_prompt(4, False), "coordinator"))), 4)
    assert none.teams == [] and none.vision_dod


def test_standards_parse():
    out = prompts.parse_standards(extract_json(content(
        ask(FakeLLM(), prompts.standards_prompt("g1", "g2"), "qj"))))
    assert out["definition_of_done"] and out["definition_of_ready"]


def test_plan_respects_scenario_and_cap_and_chain():
    fake = FakeLLM({"tasks_per_plan": 3, "plan_dependencies": "chain"})
    tasks = plan_tasks(fake, cap=4)
    assert [t.name for t in tasks] == ["Team A task 1", "Team A task 2", "Team A task 3"]
    assert [t.depends_on for t in tasks] == [[], [0], [1]]
    kinds = {c.check_kind.value for c in tasks[0].criteria}
    assert kinds == {"file_exists", "min_words", "file_contains", "rubric"}
    assert len(plan_tasks(fake, cap=2)) == 2                      # prompt limit wins
    assert plan_tasks(fake, cap=4)[0].name == "Team A task 6"      # numbering keeps counting


def test_prioritize_gives_both_fields_and_honours_slots():
    fake = FakeLLM()
    cands = [task_obj("a", "One"), task_obj("b", "Two"), task_obj("c", "Three")]
    user = prompts.prioritize_prompt(cands, [], None, [])
    res = prompts.parse_prioritize(extract_json(content(ask(fake, user))), ["a", "b", "c"])
    assert res.order == ["a", "b", "c"] and res.next_task_id == "a"
    raw = extract_json(content(ask(fake, user)))
    assert raw["next_task_ids"] == ["a"] and raw["next_task_id"] == "a"
    # the 0.2 prompt says "up to N slot(s)"
    slotted = user.replace("Job: prioritize", "Job: prioritize (up to 2 slot(s) are free)", 1)
    raw = extract_json(content(ask(fake, slotted)))
    assert raw["next_task_ids"] == ["a", "b"] and raw["next_task_id"] == "a"


def test_prioritize_can_reject_by_name():
    fake = FakeLLM({"reject_names_containing": "Two"})
    cands = [task_obj("a", "One"), task_obj("b", "Two")]
    res = prompts.parse_prioritize(extract_json(content(
        ask(fake, prompts.prioritize_prompt(cands, [], None, [])))), ["a", "b"])
    assert res.rejections[0][0] == "b" and res.next_task_id == "a"


def test_classify():
    fake = FakeLLM({"complexity": "complex"})
    assert prompts.parse_classification(extract_json(content(
        ask(fake, prompts.classify_prompt(task_obj()), "delivery")))) == "complex"


def test_quality_review_pass_and_block():
    crits = [crit(1, "intro.md exists", "file_exists", "intro.md", status="green"),
             crit(2, "Tone is friendly"), crit(3, "Has examples")]
    t = task_obj(criteria=crits)
    user = prompts.quality_review_prompt(t, crits, {"intro.md": "text"})
    ok = prompts.parse_quality_review(extract_json(content(ask(FakeLLM(), user, "qj"))), [2, 3])
    assert ok.passed and ok.quality_score == 1.0 and set(ok.criteria) == {2, 3}
    fake = FakeLLM({"review_fail_attempts": 1})
    bad = prompts.parse_quality_review(extract_json(content(ask(fake, user, "qj"))), [2, 3])
    assert not bad.passed and bad.feedback and all(not v[0] for v in bad.criteria.values())
    # the second attempt of the same task passes again
    again = prompts.parse_quality_review(extract_json(content(ask(fake, user, "qj"))), [2, 3])
    assert again.passed


def test_quality_review_blocks_on_a_red_automatic_check():
    crits = [crit(1, "intro.md exists", "file_exists", "intro.md", status="red")]
    user = prompts.quality_review_prompt(task_obj(criteria=crits), crits, {})
    res = prompts.parse_quality_review(extract_json(content(ask(FakeLLM(), user, "qj"))), [])
    assert not res.passed and "intro.md exists" in res.feedback


def test_mission_review_pass_block_and_judge_selection():
    t = task_obj()
    user = prompts.mission_review_prompt(t, "summary", {})
    ok = prompts.parse_mission_review(extract_json(content(ask(FakeLLM(), user))))
    assert ok.expected_value_met and ok.confidence >= 0.99
    quality_only = FakeLLM({"review_fail_attempts": 1})         # judge defaults to quality
    assert prompts.parse_mission_review(
        extract_json(content(ask(quality_only, user)))).expected_value_met
    fake = FakeLLM({"review_fail_attempts": 1, "review_fail_judge": "mission"})
    blocked = prompts.parse_mission_review(extract_json(content(ask(fake, user))))
    assert not blocked.expected_value_met and blocked.feedback


def test_empty_feedback_first_forces_the_reask_path():
    fake = FakeLLM({"empty_feedback_first": 1})
    user = prompts.mission_review_prompt(task_obj(), "summary", {})
    with pytest.raises(ValueError, match="feedback"):
        prompts.parse_mission_review(extract_json(content(ask(fake, user))))
    reask = [{"role": "assistant", "content": "x"},
             {"role": "user", "content": "Your reply could not be used: nope"}]
    assert prompts.parse_mission_review(extract_json(content(ask(fake, user, extra=reask)))
                                        ).expected_value_met
    assert [c["reask"] for c in fake.calls] == [False, True]


def test_vote_scenario():
    fake = FakeLLM()
    user = prompts.vote_prompt(SimpleNamespace(name="N", mission_statement="m", reason=""),
                               "A", [], [])
    assert prompts.parse_vote(extract_json(content(ask(fake, user, "delivery"))))[0] is True
    fake.configure({"vote_approve": False})
    assert prompts.parse_vote(extract_json(content(ask(fake, user, "delivery"))))[0] is False


def test_assess_accomplished_continue_restart_and_low_confidence():
    team = SimpleNamespace(name="Team A", mission_statement="m")
    user = prompts.assess_prompt(team, [], [])
    fake = FakeLLM()
    # nothing planned yet -> continue, with a concrete focus (parser requires it)
    a = prompts.parse_assessment(extract_json(content(ask(fake, user))))
    assert a.status == "continue" and a.next_focus
    plan_tasks(fake)
    a = prompts.parse_assessment(extract_json(content(ask(fake, user))))
    assert a.status == "accomplished" and a.confidence >= 0.99
    fake.configure({"accomplished_confidence": 0.5})
    a = prompts.parse_assessment(extract_json(content(ask(fake, user))))
    assert a.confidence == 0.5 and a.next_focus           # still valid under the contract
    fake.configure({"assess_restart_count": 1})
    assert prompts.parse_assessment(extract_json(content(ask(fake, user)))).status == "restart"
    assert prompts.parse_assessment(extract_json(content(ask(fake, user)))).status != "restart"
    fake.configure({"plan_rounds": 2, "accomplished_confidence": 0.995})
    assert prompts.parse_assessment(extract_json(content(ask(fake, user)))).status == "continue"


def test_review_vision_default_met_and_gaps_first():
    teams = [SimpleNamespace(name="Alpha", status="accomplished", mission_statement="m"),
             SimpleNamespace(name="Beta", status="accomplished", mission_statement="m")]
    user = prompts.vision_review_prompt("V", "dod", teams, {}, "", {})
    fake = FakeLLM({"vision_gaps_first": 1})
    first = prompts.parse_vision_review(
        extract_json(content(ask(fake, user, "coordinator"))), ["Alpha", "Beta"])
    assert first.verdict == "gaps" and first.gaps[0][0] == "Alpha" and first.gaps[0][1]
    second = prompts.parse_vision_review(
        extract_json(content(ask(fake, user, "coordinator"))), ["Alpha", "Beta"])
    assert second.verdict == "met" and second.confidence >= 0.99


def test_retrospective():
    learned = prompts.parse_retrospective(extract_json(content(
        ask(FakeLLM(), prompts.retrospective_prompt("x"), "coordinator"))))
    assert {k for k, _ in learned} == {"pattern", "routing"}


# -- work tool loop against the real tools and checks ------------------------------------------

def test_criteria_parser_reads_the_real_crit_lines():
    crits = [crit(1, "a.md exists", "file_exists", "a.md"),
             crit(2, "a.md has words", "min_words", "a.md", "30"),
             crit(3, "has [Overview] header", "file_contains", "a.md", r"\bOverview\b"),
             crit(4, "data valid", "json_valid", "d.json"), crit(5, "tone")]
    parsed = parse_criteria(prompts.work_prompt(task_obj(criteria=crits), crits, None, ""))
    assert [(c["kind"], c["path"], c["arg"]) for c in parsed] == [
        ("file_exists", "a.md", None), ("min_words", "a.md", "30"),
        ("file_contains", "a.md", r"\bOverview\b"), ("json_valid", "d.json", None)]


def _work_registry(root):
    submitted = {}
    reg = ToolRegistry()
    for tool in workspace_tools(root, reg):
        reg.add(tool)

    async def submit(args):
        submitted["summary"] = args.get("summary", "")
        return "submitted"

    async def remember(args):
        return "remembered"

    async def propose_team(args):
        submitted["proposed"] = args.get("name")
        return "proposal created"

    reg.add(Tool("submit", "finish", {"type": "object", "properties": {}}, submit, terminal=True))
    reg.add(Tool("remember", "note", {"type": "object", "properties": {}}, remember))
    reg.add(Tool("propose_team", "propose", {"type": "object", "properties": {}}, propose_team))
    return reg, submitted


async def _run_work(fake: FakeLLM, task, tmp_path, max_steps=6):
    client = LLMClient("http://fake", transport=httpx.MockTransport(fake.httpx_handler))
    reg, submitted = _work_registry(tmp_path)
    user = prompts.work_prompt(task, task.criteria, None, "")
    loop = await client.run_tool_loop("fast", system("delivery"), user, reg, max_steps)
    return loop, reg, submitted


async def test_work_loop_writes_files_that_pass_the_real_checks_then_submits(tmp_path):
    crits = [crit(1, "a.md exists", "file_exists", "a.md"),
             crit(2, "a.md long", "min_words", "a.md", "60"),
             crit(3, "has header", "file_contains", "a.md", "Executive Summary"),
             crit(4, "regex", "file_contains", "a.md", r"^# a\.md"),
             crit(5, "d valid", "json_valid", "d.json"), crit(6, "tone")]
    fake = FakeLLM()
    loop, reg, submitted = await _run_work(fake, task_obj(artifact="a.md", criteria=crits),
                                           tmp_path)
    assert loop.stopped_by == "terminal" and loop.terminal_tool == "submit"
    assert sorted(reg.files_written) == ["a.md", "d.json"]
    for c in crits:
        if c.check_kind == "rubric":
            continue
        res = run_check(AcceptanceCriterion(
            id="x", task_id="t", ordinal=c.ordinal, text=c.text, check_kind=c.check_kind,
            check_path=c.check_path, check_arg=c.check_arg), tmp_path)
        assert res.passed, (c.text, res.evidence)
    assert [c["job"] for c in fake.calls] == ["work", "work"]      # write, then submit


async def test_work_loop_uses_the_artifact_when_there_are_no_criteria(tmp_path):
    loop, reg, _ = await _run_work(FakeLLM(), task_obj(artifact="notes/x.md"), tmp_path)
    assert loop.stopped_by == "terminal" and reg.files_written == ["notes/x.md"]


async def test_work_loop_scenarios_no_submit_and_extra_tools(tmp_path):
    fake = FakeLLM({"work_submit": False})
    loop, _, _ = await _run_work(fake, task_obj(), tmp_path)
    assert loop.stopped_by == "answer"
    fake = FakeLLM({"work_remember": True, "work_propose_team": "Spin-off"})
    loop, _, submitted = await _run_work(fake, task_obj(), tmp_path)
    assert loop.stopped_by == "terminal" and submitted["proposed"] == "Spin-off"
    loop, _, submitted = await _run_work(fake, task_obj(), tmp_path)   # one-shot
    assert "proposed" not in submitted


# -- scenarios ------------------------------------------------------------------------------------

async def _client(fake):
    return LLMClient("http://fake", transport=httpx.MockTransport(fake.httpx_handler),
                     sleep=_no_sleep)


async def _no_sleep(_):
    return None


async def test_malformed_replies_exhaust_the_reask_then_recover():
    fake = FakeLLM({"malformed_next": 2})
    client = await _client(fake)
    with pytest.raises(LLMError) as ei:
        await client.complete_json("fast", system(), prompts.standards_prompt(),
                                   prompts.parse_standards)
    assert ei.value.code == "parse"
    parsed, _ = await client.complete_json("fast", system(), prompts.standards_prompt(),
                                           prompts.parse_standards)
    assert parsed["definition_of_done"]
    fake.configure({"malformed_next": 1})                       # one bad reply: the re-ask fixes it
    parsed, loop = await client.complete_json("fast", system(), prompts.standards_prompt(),
                                              prompts.parse_standards)
    assert loop.steps == 2


async def test_outage_window_gives_503_then_recovers():
    now = [100.0]
    fake = FakeLLM(clock=lambda: now[0])
    fake.configure({"outage_seconds": 30})
    client = await _client(fake)
    with pytest.raises(LLMError) as ei:
        await client.chat("fast", [{"role": "user", "content": "Job: classify"}])
    assert ei.value.code == "http" and ei.value.status == 503
    assert await _models_status(fake) == 503
    assert [c["status"] for c in fake.calls] == [503, 503, 503]   # 1 try + 2 retries
    now[0] += 31
    assert (await client.list_models()) == ["instant", "fast", "great", "amazing"]
    fake.configure({"outage_seconds": 5, "outage_status": 429})
    with pytest.raises(FakeHTTPError) as ei2:
        fake.handle({"model": "x", "messages": []})
    assert ei2.value.status == 429
    fake.configure({"outage_seconds": 0})
    assert fake.handle({"model": "x", "messages": []})["choices"]


async def _models_status(fake):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.httpx_handler)) as c:
        return (await c.get("http://fake/v1/models")).status_code


async def test_delay_and_overlap_recording():
    fake = FakeLLM({"delay_ms": 60})
    msgs = [{"role": "system", "content": system()},
            {"role": "user", "content": prompts.classify_prompt(task_obj())}]
    await asyncio.gather(*[fake.ahandle({"model": "m", "messages": msgs}) for _ in range(3)])
    assert fake.max_overlap("classify") == 3
    assert all(c["t_end"] - c["t"] >= 0.05 for c in fake.calls)


async def test_cancelled_call_is_marked():
    fake = FakeLLM({"delay_ms": 500})
    msgs = [{"role": "user", "content": prompts.classify_prompt(task_obj())}]
    t = asyncio.create_task(fake.ahandle({"model": "m", "messages": msgs}))
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert fake.calls[0]["cancelled"] is True


def test_configure_validates_and_coerces_and_reset_restores_defaults():
    fake = FakeLLM()
    with pytest.raises(ValueError, match="unknown scenario"):
        fake.configure({"nope": 1})
    out = fake.configure({"teams": "2", "vote_approve": "false", "accomplished_confidence": "0.5",
                          "team_names": ["X", "Y"]})
    assert out["teams"] == 2 and out["vote_approve"] is False
    assert out["accomplished_confidence"] == 0.5 and out["team_names"] == ["X", "Y"]
    ask(fake, prompts.standards_prompt(), "qj")
    assert fake.calls
    fake.reset()
    assert fake.calls == [] and fake.scenario_dict() == FakeLLM().scenario_dict()


def test_unknown_job_gets_a_plain_answer():
    fake = FakeLLM()
    assert content(fake.handle({"model": "m", "messages": [
        {"role": "user", "content": "hello"}]})) == "OK"
    assert fake.calls[-1]["job"] == "unknown"


async def test_models_endpoint_follows_the_scenario():
    fake = FakeLLM({"models": ["a", "b"]})
    assert await (await _client(fake)).list_models() == ["a", "b"]


async def test_server_app_exposes_control_and_openai_endpoints():
    from tests.fakes.fake_llm_server import create_app

    transport = httpx.ASGITransport(app=create_app(FakeLLM()))
    async with httpx.AsyncClient(transport=transport, base_url="http://fake") as c:
        assert (await c.get("/v1/models")).json()["data"][0]["id"] == "instant"
        r = await c.post("/v1/chat/completions", json={"model": "fast", "messages": [
            {"role": "system", "content": system("qj")},
            {"role": "user", "content": prompts.standards_prompt()}]})
        assert r.status_code == 200 and json.loads(content(r.json()))["definition_of_done"]
        calls = (await c.get("/control/calls")).json()
        assert calls["count"] == 1 and calls["calls"][0]["job"] == "standards"
        assert (await c.get("/control/calls", params={"since": 1})).json()["calls"] == []
        assert (await c.post("/control/scenario", json={"teams": 2})).json()["teams"] == 2
        assert (await c.post("/control/scenario", json={"bogus": 1})).status_code == 400
        await c.post("/control/scenario", json={"outage_seconds": 60})
        r = await c.post("/v1/chat/completions", json={"model": "m", "messages": []})
        assert r.status_code == 503
        assert (await c.post("/control/reset")).json() == {"status": "reset"}
        assert (await c.get("/control/scenario")).json()["teams"] == 1
        assert (await c.get("/health")).json() == {"status": "ok"}
