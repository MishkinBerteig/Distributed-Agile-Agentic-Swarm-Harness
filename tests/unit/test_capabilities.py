"""Agent capabilities (docs/DESIGN-0.3.md section 11): vocabulary, assignment, prompts, leasing."""
from datetime import datetime, timedelta, timezone

from app import policy, prompts, remote
from app.engine import decide
from app.models import (AcceptanceCriterion, ActionKind as K, Agent, HarnessSpec, Swarm,
                        SwarmSnapshot, Task, Team, harness_capabilities,
                        normalize_capabilities, required_capabilities)
from daash_runner import config as rconfig

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=1)


def ag(id, caps, state="idle", last=None):
    return Agent(id=id, swarm_id="s", team_id="tm", role="mission_delivery", name=id, model="fast",
                 state=state, capabilities=caps, created_at=T0, last_active_at=last)


def tk(id, status="Ready", needs=(), **kw):
    c = AcceptanceCriterion(id=f"c{id}", task_id=id, ordinal=1, text="x")
    base = dict(id=id, swarm_id="s", team_id="tm", name=f"Task {id}", status=status,
                created_at=T0, updated_at=T0, criteria=[c], required_capabilities=list(needs))
    base.update(kw)
    return Task(**base)


def snap(tasks, agents):
    sw = Swarm(id="s", name="S", vision_statement="V", definition_of_done="d",
               lifecycle_state="ACTIVE", created_at=T0, started_at=T0)
    tm = Team(id="tm", swarm_id="s", name="T", mission_statement="M", status="active",
              definition_of_done="d", created_at=T0)
    return SwarmSnapshot(swarm=sw, teams=[tm], agents=agents, tasks=tasks, now=NOW)


def commits(out):
    return [(a.task_id, a.agent_id) for a in out if a.kind == K.COMMIT_TASK]


NATIVE = ["edit_files"]
REMOTE = ["edit_files", "run_code"]


# -- vocabulary and defaults ---------------------------------------------------------------

def test_harness_defaults_and_override():
    assert harness_capabilities("daash") == ["edit_files"]
    for h in ("openclaude", "claude-code", "codex", "fake"):
        assert harness_capabilities(h) == REMOTE
    assert harness_capabilities("daash", ["run_code", "bogus", "run_code"]) == ["run_code"]
    assert harness_capabilities("unknown") == ["edit_files"]
    assert HarnessSpec().capabilities is None


def test_normalize_and_required():
    assert normalize_capabilities(["Run_Code", "x", 3]) == ["run_code"]
    assert normalize_capabilities("run_code") == ["run_code"]
    assert required_capabilities(["edit_files", "web_research", "nope"]) == ["web_research"]


# -- assignment -------------------------------------------------------------------------------

def test_run_code_task_skips_native_agent():
    out = decide(snap([tk("t1", needs=["run_code"])], [ag("native", NATIVE), ag("remote", REMOTE)]))
    assert commits(out) == [("t1", "remote")]


def test_capable_agent_busy_next_task_goes_to_native():
    tasks = [tk("t1", needs=["run_code"], priority=1), tk("t2", priority=2)]
    out = decide(snap(tasks, [ag("native", NATIVE), ag("remote", REMOTE, state="busy")]))
    assert commits(out) == [("t2", "native")]


def test_only_task_waits_for_busy_capable_agent():
    out = decide(snap([tk("t1", needs=["run_code"])],
                      [ag("native", NATIVE), ag("remote", REMOTE, state="busy")]))
    assert commits(out) == []


def test_degraded_when_nobody_can():
    out = decide(snap([tk("t1", needs=["run_code"])], [ag("native", NATIVE)]))
    assert commits(out) == [("t1", "native")]


def test_rotation_order_kept_among_capable():
    a = ag("a", REMOTE, last=T0 + timedelta(minutes=5))
    b = ag("b", REMOTE, last=T0)
    assert commits(decide(snap([tk("t1", needs=["run_code"])], [a, b]))) == [("t1", "b")]


def test_dor_reason_wording():
    agents = [ag("native", NATIVE), ag("remote", REMOTE, state="busy")]
    t = tk("t1", needs=["run_code"])
    idle_only = [ag("native", NATIVE), ag("remote", REMOTE)]
    # the only capable agent is busy -> reason mentions capabilities
    res = policy.dor_check(t, [t], agents, None, NOW)
    assert not res.ok and any("capabilities" in r and "run_code" in r for r in res.reasons)
    assert policy.dor_check(t, [t], idle_only, None, NOW).ok
    assert policy.dor_check(tk("t2"), [tk("t2")], agents, None, NOW).ok


def test_can_take_and_lacks():
    t = tk("t1", needs=["run_code", "web_research"])
    n = ag("n", NATIVE)
    assert policy.lacks(n, t) == ["run_code", "web_research"]
    assert policy.can_take(n, t, [n])                                  # degraded
    assert not policy.can_take(n, t, [n, ag("w", ["edit_files", "run_code", "web_research"])])


# -- plan contract and prompts ------------------------------------------------------------------

def test_parse_plan_needs_dropping_unknown():
    plan = prompts.parse_plan({"tasks": [
        {"name": "Verify", "acceptance_criteria": [{"text": "ok"}],
         "needs": ["run_code", "edit_files", "teleport"]},
        {"name": "Write", "acceptance_criteria": [{"text": "ok"}]}]}, 4)
    assert plan[0].needs == ["run_code"] and plan[1].needs == []


def test_plan_prompt_shows_team_capabilities():
    p = prompts.plan_prompt("T", [], [], 3, None, "", [], agent_capabilities=[NATIVE, REMOTE])
    assert "Delivery agent 2: edit_files, run_code" in p and '"needs"' in p
    p = prompts.plan_prompt("T", [], [], 3, None, "", [], agent_capabilities=[NATIVE])
    assert "leave needs empty" in p


def test_work_prompts_state_capabilities():
    t = tk("t1", needs=["run_code"])
    p = prompts.work_prompt(t, t.criteria, None, "", capabilities=NATIVE)
    assert "Your capabilities: edit_files" in p and "You cannot run shell commands" in p
    assert "needs run_code, which you do not have" in p
    r = prompts.remote_work_prompt(t, t.criteria, None, "", capabilities=REMOTE)
    assert "run_code (run shell commands" in r and "You cannot search and fetch the web" in r
    assert "which you do not have" not in r
    assert "Your capabilities" not in prompts.work_prompt(t, t.criteria, None, "")


def test_review_prompts_state_author_capabilities():
    t = tk("t1")
    q = prompts.quality_review_prompt(t, t.criteria, {}, NATIVE)
    m = prompts.mission_review_prompt(t, "s", {}, NATIVE)
    for p in (q, m):
        assert "delivering agent's capabilities: edit_files" in p and "could not use: run_code" in p
    assert "delivering agent" not in prompts.quality_review_prompt(t, t.criteria, {})


# -- lease matching and runner side ---------------------------------------------------------------

def test_job_matches_with_capabilities():
    caps = {"fake": []}
    assert not remote.job_matches("fake", "m", caps, ["run_code"], {"fake": ["edit_files"]})
    assert remote.job_matches("fake", "m", caps, ["run_code"], {"fake": REMOTE})
    assert remote.job_matches("fake", "m", caps, ["run_code"], {})          # default = run_code
    assert remote.job_matches("fake", "m", caps, [], {"fake": ["edit_files"]})
    jobs = [{"id": 1, "harness": "fake", "model": "m",
             "payload": {"required_capabilities": ["run_code"]}},
            {"id": 2, "harness": "fake", "model": "m", "payload": {}}]
    assert remote.first_match(jobs, caps, {"fake": ["edit_files"]})["id"] == 2
    assert remote.first_match(jobs, caps, {"fake": REMOTE})["id"] == 1


def test_hello_normalizes_capabilities():
    out = remote.normalize_capabilities([
        {"harness": "codex", "models": [], "capabilities": ["run_code", "x"]}, {"harness": "fake"}])
    assert out[0]["capabilities"] == ["run_code"] and out[1]["capabilities"] == REMOTE
    assert remote.harness_offered([{"harness": "codex"},
                                   {"harness": "fake", "capabilities": ["edit_files"]}]) == {
        "codex": REMOTE, "fake": ["edit_files"]}


def test_runner_default_capabilities():
    f = rconfig.capabilities_of
    assert f("codex", {}) == REMOTE
    assert f("claude-code", {"permission_mode": "acceptEdits"}) == ["edit_files"]
    assert f("openclaude", {"permission_mode": "plan"}) == ["edit_files"]
    assert f("claude-code", {"permission_mode": "bypassPermissions"}) == REMOTE
    assert f("codex", {"sandbox": "read-only"}) == ["edit_files"]
    assert f("codex", {"sandbox": "workspace-write"}) == REMOTE
    assert f("codex", {"capabilities": ["web_research", "edit_files", "zzz"]}) == [
        "edit_files", "web_research"]
