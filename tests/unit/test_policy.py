from datetime import datetime, timedelta, timezone

import pytest

from app import policy
from app.models import (SWARM_ACTIONS, AcceptanceCriterion, Agent, DoDThresholds,
                        Limits, Swarm, SwarmConfig, Task)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def mk_task(id="t1", status="Ready", **kw):
    base = dict(id=id, swarm_id="s", team_id="tm", name=f"Task {id}", status=status,
                created_at=T0, updated_at=T0)
    base.update(kw)
    return Task(**base)


def crit(task_id="t1"):
    return AcceptanceCriterion(id="c", task_id=task_id, ordinal=1, text="x")


def mk_agent(id="a1", role="mission_delivery", state="idle"):
    return Agent(id=id, swarm_id="s", team_id="tm", role=role, name=id, model="fast",
                 state=state, created_at=T0)


def done_task(id, secs, done_at):
    return mk_task(id, "Done", committed_at=done_at - timedelta(seconds=secs), done_at=done_at)


def mk_swarm(**limits):
    return Swarm(id="s", name="S", vision_statement="v", created_at=T0,
                 started_at=T0, config=SwarmConfig(limits=Limits(**limits)))


# --- lifecycle ---------------------------------------------------------------

def test_transition_all_declared_moves():
    for action, moves in SWARM_ACTIONS.items():
        for frm, to in moves.items():
            assert policy.transition(frm, action) == to


def test_transition_unknown_action():
    with pytest.raises(ValueError, match="Unknown swarm action 'explode'"):
        policy.transition("ACTIVE", "explode")


def test_transition_illegal_mentions_allowed():
    with pytest.raises(ValueError) as e:
        policy.transition("CREATED", "pause")
    assert "CREATED" in str(e.value) and "start" in str(e.value)


def test_transition_terminal_state():
    with pytest.raises(ValueError, match="terminal"):
        policy.transition("ARCHIVED", "start")


def test_allowed_actions():
    assert policy.allowed_actions("CREATED") == ["start", "delete"]
    assert policy.allowed_actions("PAUSED") == ["resume", "feedback", "verify"]
    assert policy.allowed_actions("ARCHIVED") == []
    assert policy.allowed_actions("DELETED") == []


# --- task transitions --------------------------------------------------------

@pytest.mark.parametrize("frm,to,ok", [
    ("Pending", "Ready", True), ("Pending", "Rejected", True), ("Pending", "Done", False),
    ("Ready", "In-Progress", True), ("Ready", "Pending", True), ("Ready", "Review", False),
    ("In-Progress", "Review", True), ("In-Progress", "In-Progress", True),
    ("In-Progress", "Failed", True), ("In-Progress", "Done", False),
    ("Review", "Done", True), ("Review", "In-Progress", True), ("Review", "Failed", True),
    ("Review", "Ready", False),
    ("Done", "Pending", False), ("Failed", "Ready", False), ("Rejected", "Pending", False),
    ("Bogus", "Ready", False),
])
def test_can_transition_task(frm, to, ok):
    assert policy.can_transition_task(frm, to) is ok


def test_terminal_task_states_have_no_exits():
    for s in ("Done", "Failed", "Rejected"):
        assert policy.TASK_TRANSITIONS[s] == set()


# --- p90 ---------------------------------------------------------------------

def test_p90_empty_and_single():
    assert policy.p90([]) is None
    assert policy.p90([7.0]) == 7.0


def test_p90_nearest_rank():
    assert policy.p90([float(i) for i in range(1, 11)]) == 9.0     # ceil(9.0)=9th
    assert policy.p90([float(i) for i in range(1, 12)]) == 10.0    # ceil(9.9)=10th
    assert policy.p90([float(i) for i in range(1, 21)]) == 18.0
    assert policy.p90([5.0, 1.0, 3.0]) == 5.0                      # unsorted input, ceil(2.7)=3


def test_trailing_p90_requires_min_samples():
    tasks = [done_task(f"d{i}", 10, T0 + timedelta(hours=i)) for i in range(4)]
    assert policy.trailing_p90_cycle_seconds(tasks) is None
    tasks.append(done_task("d4", 10, T0 + timedelta(hours=4)))
    assert policy.trailing_p90_cycle_seconds(tasks) == 10.0


def test_trailing_p90_ignores_non_done_and_missing_timestamps():
    tasks = [done_task(f"d{i}", 10, T0 + timedelta(hours=i)) for i in range(5)]
    tasks.append(mk_task("ip", "In-Progress", committed_at=T0))
    tasks.append(mk_task("nodone", "Done", committed_at=T0))          # no done_at
    tasks.append(mk_task("nocommit", "Done", done_at=T0))             # no committed_at
    assert policy.trailing_p90_cycle_seconds(tasks) == 10.0
    assert policy.trailing_p90_cycle_seconds(tasks[:4]) is None


def test_trailing_p90_window_uses_most_recent():
    old = [done_task(f"o{i}", 1000, T0 + timedelta(minutes=i)) for i in range(10)]
    new = [done_task(f"n{i}", 5, T0 + timedelta(days=1, minutes=i)) for i in range(5)]
    assert policy.trailing_p90_cycle_seconds(old + new, window=5) == 5.0
    assert policy.trailing_p90_cycle_seconds(old + new, window=15) == 1000.0


def test_trailing_p90_custom_min_samples_and_empty():
    assert policy.trailing_p90_cycle_seconds([], min_samples=1) is None
    t = [done_task("a", 42, T0)]
    assert policy.trailing_p90_cycle_seconds(t, min_samples=1) == 42.0


# --- helpers -----------------------------------------------------------------

def test_deps_done():
    a = mk_task("a", "Done")
    b = mk_task("b", "Ready")
    by_id = {"a": a, "b": b}
    assert policy.deps_done(mk_task("x", depends_on=[]), by_id)
    assert policy.deps_done(mk_task("x", depends_on=["a"]), by_id)
    assert not policy.deps_done(mk_task("x", depends_on=["a", "b"]), by_id)
    assert not policy.deps_done(mk_task("x", depends_on=["ghost"]), by_id)


def test_idle_delivery_agents():
    agents = [mk_agent("a1"), mk_agent("a2"), mk_agent("a3", state="busy"),
              mk_agent("j", role="mission_judgement"), mk_agent("a4"),
              mk_agent("a5", state="stopped")]
    tasks = [mk_task("t1", "In-Progress", assigned_agent_id="a2"),
             mk_task("t2", "Review", assigned_agent_id="a4"),
             mk_task("t3", "Done", assigned_agent_id="a1")]
    assert [a.id for a in policy.idle_delivery_agents(agents, tasks)] == ["a1"]
    assert policy.idle_delivery_agents([], tasks) == []


def test_overdue_in_progress_boundary_and_none():
    t = mk_task("t", "In-Progress", committed_at=T0)
    now = T0 + timedelta(seconds=100)
    assert policy.overdue_in_progress([t], None, now) == []
    assert policy.overdue_in_progress([t], 100, now) == []           # exactly p90 is not older
    assert policy.overdue_in_progress([t], 99.9, now) == [t]
    assert policy.overdue_in_progress([mk_task("r", "Review", committed_at=T0)], 1, now) == []
    assert policy.overdue_in_progress([mk_task("n", "In-Progress")], 1, now) == []


def test_next_ready_task_priority_and_ties():
    t_a = mk_task("a", priority=2, approved_at=T0)
    t_b = mk_task("b", priority=1, approved_at=T0 + timedelta(hours=1))
    t_c = mk_task("c", priority=None, approved_at=T0)
    assert policy.next_ready_task([t_a, t_b, t_c]).id == "b"
    assert policy.next_ready_task([t_a, t_c]).id == "a"               # None priority last
    assert policy.next_ready_task([t_c]).id == "c"


def test_next_ready_task_ties_by_approved_then_created():
    e = mk_task("e", priority=1, approved_at=T0 + timedelta(hours=2))
    l = mk_task("l", priority=1, approved_at=T0 + timedelta(hours=1))
    assert policy.next_ready_task([e, l]).id == "l"
    n = mk_task("n", priority=1, approved_at=None)
    assert policy.next_ready_task([n, e]).id == "e"                   # no approved_at last
    c1 = mk_task("c1", priority=1, created_at=T0 + timedelta(minutes=5))
    c2 = mk_task("c2", priority=1, created_at=T0)
    assert policy.next_ready_task([c1, c2]).id == "c2"


def test_next_ready_task_skips_unmet_deps_and_other_statuses():
    dep = mk_task("dep", "In-Progress")
    blocked = mk_task("b", priority=1, depends_on=["dep"])
    free = mk_task("f", priority=5)
    pend = mk_task("p", "Pending", priority=0)
    assert policy.next_ready_task([dep, blocked, free, pend]).id == "f"
    assert policy.next_ready_task([dep, blocked, pend]) is None
    assert policy.next_ready_task([]) is None


# --- DoR ---------------------------------------------------------------------

def ready_scenario():
    task = mk_task("t1", priority=1, criteria=[crit()])
    return task, [task], [mk_agent()]


def test_dor_ok():
    task, tasks, agents = ready_scenario()
    r = policy.dor_check(task, tasks, agents, None, T0)
    assert r.ok and r.reasons == []


def test_dor_no_idle_agent():
    task, tasks, _ = ready_scenario()
    r = policy.dor_check(task, tasks, [mk_agent(state="busy")], None, T0)
    assert not r.ok and len(r.reasons) == 1 and "idle" in r.reasons[0]


def test_dor_agent_with_in_progress_task_not_free():
    task, tasks, agents = ready_scenario()
    tasks.append(mk_task("w", "In-Progress", assigned_agent_id="a1", committed_at=T0))
    r = policy.dor_check(task, tasks, agents, None, T0)
    assert not r.ok


def test_dor_overdue_in_progress_blocks():
    task, tasks, agents = ready_scenario()
    tasks.append(mk_task("slow", "In-Progress", assigned_agent_id="other", committed_at=T0))
    now = T0 + timedelta(seconds=601)
    r = policy.dor_check(task, tasks, agents, 600, now)
    assert not r.ok and any("Task slow" in x and "p90" in x for x in r.reasons)
    assert policy.dor_check(task, tasks, agents, 600, T0 + timedelta(seconds=600)).ok
    assert policy.dor_check(task, tasks, agents, None, now).ok       # no p90 => no constraint


def test_dor_must_be_ready_and_next():
    task, tasks, agents = ready_scenario()
    pend = mk_task("t1", "Pending", criteria=[crit()])
    r = policy.dor_check(pend, [pend], agents, None, T0)
    assert not r.ok and "not Ready" in r.reasons[0]

    higher = mk_task("hi", priority=0, criteria=[crit("hi")])
    r = policy.dor_check(task, tasks + [higher], agents, None, T0)
    assert not r.ok and "next highest-priority" in r.reasons[0] and "Task hi" in r.reasons[0]


def test_dor_task_not_in_team_tasks_list_still_judged():
    task, _, agents = ready_scenario()
    assert policy.dor_check(task, [], agents, None, T0).ok


def test_dor_criteria_and_dependencies():
    bare = mk_task("t1", priority=1)
    agents = [mk_agent()]
    r = policy.dor_check(bare, [bare], agents, None, T0)
    assert not r.ok and "no acceptance criteria" in r.reasons[0]

    dep = mk_task("d", "In-Progress", committed_at=T0)
    t = mk_task("t1", priority=1, criteria=[crit()], depends_on=["d", "ghost"])
    r = policy.dor_check(t, [t, dep], agents, None, T0)
    assert not r.ok
    joined = " ".join(r.reasons)
    assert "d, ghost" in joined


def test_dor_collects_multiple_reasons():
    t = mk_task("t1", "Pending")
    r = policy.dor_check(t, [t], [], None, T0)
    assert len(r.reasons) == 3 and all(x.endswith((".", ";")) or "." in x for x in r.reasons)


# --- DoD ---------------------------------------------------------------------

TH = DoDThresholds()


def test_dod_all_pass():
    r = policy.evaluate_dod([True, True], [1.0, 0.9995], True, True, 0.995, TH)
    assert r.passed and r.quality_passed and r.mission_passed and r.reasons == []
    assert r.quality_score == pytest.approx((1 + 1 + 1.0 + 0.9995) / 4)


def test_dod_vacuous_cases():
    r = policy.evaluate_dod([], [], True, True, 1.0, TH)
    assert r.passed and r.quality_score == 1.0
    assert policy.evaluate_dod([True], [], True, True, 1.0, TH).passed
    assert policy.evaluate_dod([], [1.0], True, True, 1.0, TH).passed


def test_dod_no_criteria_but_qj_failed():
    r = policy.evaluate_dod([], [], False, True, 1.0, TH)
    assert not r.passed and not r.quality_passed and r.mission_passed
    assert r.quality_score == 0.0


def test_dod_deterministic_failure():
    r = policy.evaluate_dod([True, False], [], True, True, 1.0, TH)
    assert not r.quality_passed and not r.passed
    assert r.quality_score == 0.5
    assert "1 of 2 deterministic" in r.reasons[0]


def test_dod_deterministic_threshold_lower():
    th = DoDThresholds(deterministic=0.5)
    assert policy.evaluate_dod([True, False], [], True, True, 1.0, th).passed


def test_dod_rubric_boundary_0999():
    assert policy.evaluate_dod([], [0.999], True, True, 1.0, TH).passed
    r = policy.evaluate_dod([], [0.9989], True, True, 1.0, TH)
    assert not r.passed and "Rubric criterion 1" in r.reasons[0]
    r = policy.evaluate_dod([], [1.0, 0.5, 0.99], True, True, 1.0, TH)
    assert sum("Rubric criterion" in x for x in r.reasons) == 2


def test_dod_mission_boundary_099():
    assert policy.evaluate_dod([], [], True, True, 0.99, TH).passed
    r = policy.evaluate_dod([], [], True, True, 0.9899, TH)
    assert r.quality_passed and not r.mission_passed and not r.passed
    assert "confidence" in r.reasons[0]


def test_dod_mission_not_met_even_with_high_confidence():
    r = policy.evaluate_dod([], [], True, False, 1.0, TH)
    assert not r.mission_passed and "not met" in r.reasons[0]


def test_dod_qj_failed_blocks():
    r = policy.evaluate_dod([True], [1.0], False, True, 1.0, TH)
    assert not r.passed and not r.quality_passed
    assert any("Quality Judgement" in x for x in r.reasons)


def test_dod_both_levels_fail():
    r = policy.evaluate_dod([False], [0.1], False, False, 0.0, TH)
    assert not r.quality_passed and not r.mission_passed
    assert len(r.reasons) >= 4


# --- rework / voting ---------------------------------------------------------

def test_rework_or_fail():
    assert policy.rework_or_fail(1, 3) == "rework"
    assert policy.rework_or_fail(2, 3) == "rework"
    assert policy.rework_or_fail(3, 3) == "failed"
    assert policy.rework_or_fail(4, 3) == "failed"
    assert policy.rework_or_fail(1, 1) == "failed"


@pytest.mark.parametrize("n,q", [(0, 1), (1, 1), (2, 2), (3, 2), (4, 3), (5, 3)])
def test_majority_quorum(n, q):
    assert policy.majority_quorum(n) == q


def test_tally():
    assert policy.tally({}, 3) == "pending"
    assert policy.tally({"a": True}, 3) == "pending"
    assert policy.tally({"a": True, "b": True}, 3) == "approved"
    assert policy.tally({"a": False, "b": False}, 3) == "rejected"
    assert policy.tally({"a": False, "b": True}, 3) == "pending"
    assert policy.tally({"a": True}, 1) == "approved"
    assert policy.tally({"a": False}, 1) == "rejected"
    assert policy.tally({"a": True}, 2) == "pending"
    assert policy.tally({"a": True, "b": False}, 2) == "rejected"      # 2-voter tie => reject
    assert policy.tally({"a": False}, 4) == "pending"
    assert policy.tally({"a": False, "b": False}, 4) == "rejected"     # only 2 left, quorum 3
    assert policy.tally({}, 0) == "rejected"                           # nobody can approve


# --- cost and limits ---------------------------------------------------------

def test_cost_usd():
    assert policy.cost_usd({}, {}) == 0.0
    assert policy.cost_usd({"fast": 2000, "great": 500}, {"fast": 0.5, "great": 2.0}) == pytest.approx(2.0)
    assert policy.cost_usd({"free": 10_000}, {"fast": 1.0}) == 0.0     # unpriced = free


def test_limits_none_set():
    sw = mk_swarm(max_total_tokens=None)
    assert policy.limits_exceeded(sw, 10**9, 10**6, T0 + timedelta(days=99)) == []


def test_limits_tokens_boundary():
    sw = mk_swarm(max_total_tokens=1000)
    assert policy.limits_exceeded(sw, 999, 0, T0) == []
    r = policy.limits_exceeded(sw, 1000, 0, T0)
    assert len(r) == 1 and "max_total_tokens" in r[0] and "1,000" in r[0]


def test_limits_wall_clock():
    sw = mk_swarm(max_wall_clock_minutes=60, max_total_tokens=None)
    assert policy.limits_exceeded(sw, 0, 0, T0 + timedelta(minutes=59)) == []
    r = policy.limits_exceeded(sw, 0, 0, T0 + timedelta(minutes=61))
    assert len(r) == 1 and "max_wall_clock_minutes" in r[0]


def test_limits_wall_clock_not_started():
    sw = mk_swarm(max_wall_clock_minutes=1, max_total_tokens=None)
    sw.started_at = None
    assert policy.limits_exceeded(sw, 0, 0, T0 + timedelta(days=1)) == []


def test_limits_budget_and_multiple():
    sw = mk_swarm(max_budget_usd=5.0, max_total_tokens=100, max_wall_clock_minutes=1)
    assert policy.limits_exceeded(sw, 0, 4.99, T0) == []
    r = policy.limits_exceeded(sw, 100, 5.0, T0 + timedelta(minutes=2))
    assert len(r) == 3
    assert any("max_budget_usd" in x for x in r)
