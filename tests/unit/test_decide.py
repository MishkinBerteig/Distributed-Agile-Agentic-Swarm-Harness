"""decide() on hand-built snapshots (no DB, no network). docs/DESIGN-0.1.md section 7.1."""
from datetime import datetime, timedelta, timezone

from app.engine import decide
from app.models import (AcceptanceCriterion, Action, ActionKind as K, Agent, Limits, Swarm,
                        SwarmConfig, SwarmSnapshot, Task, Team, TeamProposal, job_key)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=1)


def swarm(state="ACTIVE", dod="done means done", **kw):
    return Swarm(id="s", name="S", vision_statement="V", definition_of_done=dod,
                 lifecycle_state=state, created_at=T0, started_at=T0, **kw)


def team(id="tm", status="active", dod="team dod", **kw):
    return Team(id=id, swarm_id="s", name=f"Team {id}", mission_statement="M", status=status,
                definition_of_done=dod, created_at=T0, **kw)


def agent(id="d1", team_id="tm", role="mission_delivery", state="idle"):
    return Agent(id=id, swarm_id="s", team_id=team_id, role=role, name=id, model="fast",
                 state=state, created_at=T0)


def task(id, status="Pending", team_id="tm", **kw):
    crit = AcceptanceCriterion(id=f"c-{id}", task_id=id, ordinal=1, text="x")
    base = dict(id=id, swarm_id="s", team_id=team_id, name=f"Task {id}", status=status,
                created_at=T0, updated_at=T0, criteria=[crit])
    base.update(kw)
    return Task(**base)


def snap(tasks=(), teams=None, agents=None, sw=None, **kw):
    teams = [team()] if teams is None else teams
    agents = [agent()] if agents is None else agents
    return SwarmSnapshot(swarm=sw or swarm(), teams=teams, agents=agents, tasks=list(tasks),
                         now=NOW, **kw)


def kinds(actions):
    return [a.kind for a in actions]


# -- lifecycle gating -----------------------------------------------------------

def test_learning_without_retrospective():
    out = decide(snap(sw=swarm("LEARNING")))
    assert kinds(out) == [K.RETROSPECTIVE]


def test_learning_after_retrospective_does_nothing():
    assert decide(snap(sw=swarm("LEARNING", retrospective_done=True))) == []


def test_non_active_states_do_nothing():
    for state in ("CREATED", "PAUSED", "USER_FEEDBACK", "VERIFICATION", "ARCHIVED"):
        assert decide(snap([task("t1", "Review")], sw=swarm(state))) == []


def test_limits_pause_and_nothing_else():
    sw = swarm(config=SwarmConfig(limits=Limits(max_total_tokens=100)))
    out = decide(snap([task("t1", "Review")], sw=sw, tokens_used=100))
    assert kinds(out) == [K.PAUSE_FOR_LIMITS]
    assert "Token limit" in out[0].detail


def test_wall_clock_limit_pauses():
    sw = swarm(config=SwarmConfig(limits=Limits(max_wall_clock_minutes=30)))
    assert kinds(decide(snap(sw=sw))) == [K.PAUSE_FOR_LIMITS]


# -- bootstrap ------------------------------------------------------------------

def test_bootstrap_when_dod_missing():
    out = decide(snap([task("t1", "Review")], sw=swarm(dod=None)))
    assert kinds(out) == [K.BOOTSTRAP]


def test_bootstrap_when_no_teams():
    assert kinds(decide(snap(teams=[], agents=[]))) == [K.BOOTSTRAP]


# -- materialize ----------------------------------------------------------------

def proposal(status="approved", created=None):
    return TeamProposal(id="p1", swarm_id="s", team_id="tm", proposer_agent_id="d1", name="N",
                        mission_statement="M", status=status, created_team_id=created,
                        created_at=T0)


def test_materialize_approved_proposal():
    out = decide(snap([task("t1", "In-Progress", assigned_agent_id="d1")],
                      proposals=[proposal()]))
    assert K.MATERIALIZE_TEAM in kinds(out)
    mat = next(a for a in out if a.kind == K.MATERIALIZE_TEAM)
    assert mat.proposal_id == "p1"


def test_no_materialize_for_pending_rejected_or_created():
    for p in (proposal("pending"), proposal("rejected"), proposal(created="x")):
        assert K.MATERIALIZE_TEAM not in kinds(decide(snap(proposals=[p])))


# -- standards ------------------------------------------------------------------

def test_standards_gate_the_team():
    out = decide(snap([task("t1", "Review")], teams=[team(dod=None)]))
    assert kinds(out) == [K.GENERATE_STANDARDS]


def test_standards_for_one_team_do_not_block_another():
    teams = [team("a", dod=None), team("b")]
    out = decide(snap([task("t1", "Review", team_id="b")], teams=teams,
                      agents=[agent("d1", "a"), agent("d2", "b")]))
    assert (K.GENERATE_STANDARDS, "a") in [(a.kind, a.team_id) for a in out]
    assert (K.REVIEW_TASK, "b") in [(a.kind, a.team_id) for a in out]


def test_inactive_team_is_skipped():
    out = decide(snap([task("t1", "Review")], teams=[team(status="accomplished")]))
    assert K.REVIEW_TASK not in kinds(out)


# -- review / work ----------------------------------------------------------------

def test_review_and_work_actions():
    tasks = [task("r", "Review", assigned_agent_id="d1"),
             task("w", "In-Progress", assigned_agent_id="d2")]
    out = decide(snap(tasks, agents=[agent("d1", state="busy"), agent("d2", state="busy")]))
    assert [(a.kind, a.task_id) for a in out] == [(K.REVIEW_TASK, "r"), (K.WORK_TASK, "w")]
    assert out[1].agent_id == "d2"


# -- commit ---------------------------------------------------------------------

def test_commit_ready_task_to_idle_agent():
    out = decide(snap([task("t1", "Ready", priority=1)]))
    assert [(a.kind, a.task_id, a.agent_id) for a in out] == [(K.COMMIT_TASK, "t1", "d1")]


def test_no_commit_when_agent_busy_with_in_progress():
    tasks = [task("w", "In-Progress", assigned_agent_id="d1"), task("t1", "Ready", priority=1)]
    out = decide(snap(tasks, agents=[agent("d1", state="busy")]))
    assert K.COMMIT_TASK not in kinds(out)


def test_commit_respects_priority_order():
    tasks = [task("lo", "Ready", priority=5), task("hi", "Ready", priority=1)]
    out = decide(snap(tasks))
    assert [a.task_id for a in out if a.kind == K.COMMIT_TASK] == ["hi"]


def test_one_commit_per_idle_agent_and_no_duplicate_task():
    tasks = [task("a", "Ready", priority=1), task("b", "Ready", priority=2),
             task("c", "Ready", priority=3)]
    out = decide(snap(tasks, agents=[agent("d1"), agent("d2")]))
    commits = [(a.task_id, a.agent_id) for a in out if a.kind == K.COMMIT_TASK]
    assert commits == [("a", "d1"), ("b", "d2")]


def test_one_ready_task_two_agents_commits_once():
    out = decide(snap([task("a", "Ready", priority=1)], agents=[agent("d1"), agent("d2")]))
    assert [a.task_id for a in out if a.kind == K.COMMIT_TASK] == ["a"]


def test_commit_blocked_by_p90_overdue_task():
    tasks = [task("w", "In-Progress", assigned_agent_id="d9",
                  committed_at=NOW - timedelta(minutes=30)),
             task("t1", "Ready", priority=1)]
    out = decide(snap(tasks, agents=[agent("d1"), agent("d9", state="busy")],
                      p90_cycle_seconds=60.0))
    assert K.COMMIT_TASK not in kinds(out)


# -- dependencies ------------------------------------------------------------------

def test_ready_task_with_unfinished_dependency_is_not_committed_and_pending_is_prioritized():
    tasks = [task("a", "Pending"),
             task("b", "Ready", priority=1, depends_on=["a"])]
    out = decide(snap(tasks))
    assert K.COMMIT_TASK not in kinds(out)
    assert kinds(out) == [K.PRIORITIZE]


def test_blocked_pending_alone_is_not_prioritized_and_team_waits():
    tasks = [task("a", "In-Progress", assigned_agent_id="d1"),
             task("b", "Pending", depends_on=["a"])]
    out = decide(snap(tasks, agents=[agent("d1", state="busy")]))
    assert K.PRIORITIZE not in kinds(out)


def test_dependency_done_makes_pending_a_candidate():
    tasks = [task("a", "Done", updated_at=T0), task("b", "Pending", depends_on=["a"])]
    assert kinds(decide(snap(tasks))) == [K.PRIORITIZE]


# -- prioritize ---------------------------------------------------------------------

def test_prioritize_when_no_ready_task():
    out = decide(snap([task("a"), task("b")]))
    assert kinds(out) == [K.PRIORITIZE]
    assert out[0].team_id == "tm"


def test_no_prioritize_when_a_startable_ready_task_exists():
    out = decide(snap([task("a", "Ready", priority=1), task("b")]))
    assert kinds(out) == [K.COMMIT_TASK]


def test_prioritize_while_another_task_is_in_progress():
    tasks = [task("w", "In-Progress", assigned_agent_id="d1"), task("b")]
    out = decide(snap(tasks, agents=[agent("d1", state="busy")]))
    assert kinds(out) == [K.WORK_TASK, K.PRIORITIZE]


# -- plan / assess / fail team ---------------------------------------------------------

def test_plan_when_team_has_no_tasks():
    assert kinds(decide(snap([]))) == [K.PLAN_TASKS]


def test_plan_not_while_open_tasks_exist():
    assert K.PLAN_TASKS not in kinds(decide(snap([task("a", "Review")])))


def test_assess_after_terminal_task_never_assessed():
    out = decide(snap([task("a", "Done")]))
    assert kinds(out) == [K.ASSESS_MISSION]


def test_assess_again_after_newer_terminal_task():
    tasks = [task("a", "Done", updated_at=T0 + timedelta(minutes=10))]
    t = team(last_assessed_at=T0 + timedelta(minutes=5))
    assert kinds(decide(snap(tasks, teams=[t]))) == [K.ASSESS_MISSION]


def test_plan_after_assessment_covers_all_terminal_tasks():
    tasks = [task("a", "Done", updated_at=T0 + timedelta(minutes=10))]
    t = team(last_assessed_at=T0 + timedelta(minutes=20), next_focus="more")
    assert kinds(decide(snap(tasks, teams=[t]))) == [K.PLAN_TASKS]


def test_rejected_only_history_plans_without_assessment():
    assert kinds(decide(snap([task("a", "Rejected")]))) == [K.PLAN_TASKS]


def limited(n):
    return swarm(config=SwarmConfig(limits=Limits(max_tasks_per_team=n)))


def test_fail_team_when_budget_exhausted_and_already_assessed():
    tasks = [task("a", "Done"), task("b", "Failed")]
    t = team(last_assessed_at=T0 + timedelta(minutes=30))
    out = decide(snap(tasks, teams=[t], sw=limited(2)))
    assert kinds(out) == [K.FAIL_TEAM]


def test_budget_exhausted_but_unassessed_gets_one_last_assessment():
    out = decide(snap([task("a", "Done"), task("b", "Done")], sw=limited(2)))
    assert kinds(out) == [K.ASSESS_MISSION]


def test_budget_exhausted_with_open_task_keeps_working():
    out = decide(snap([task("a", "Done"), task("b", "Review")], sw=limited(2)))
    assert kinds(out) == [K.REVIEW_TASK]


# -- complete swarm -------------------------------------------------------------------------

def test_complete_swarm_when_all_teams_terminal():
    teams = [team("a", status="accomplished"), team("b", status="failed")]
    out = decide(snap(teams=teams, agents=[]))
    assert kinds(out) == [K.COMPLETE_SWARM]


def test_no_complete_swarm_while_a_team_is_active():
    teams = [team("a", status="accomplished"), team("b")]
    assert K.COMPLETE_SWARM not in kinds(decide(snap(teams=teams, agents=[agent("d", "b")])))


# -- running-job suppression ---------------------------------------------------------------

def test_running_jobs_are_suppressed():
    tasks = [task("r", "Review"), task("w", "In-Progress", assigned_agent_id="d1"),
             task("p")]
    running = {job_key(K.REVIEW_TASK, "tm", "r"), job_key(K.PRIORITIZE, "tm")}
    out = decide(snap(tasks, agents=[agent("d1", state="busy")], running_jobs=running))
    assert kinds(out) == [K.WORK_TASK]


def test_running_suppresses_commit_plan_bootstrap_and_retrospective():
    assert decide(snap([task("a", "Ready", priority=1)],
                       running_jobs={job_key(K.COMMIT_TASK, "tm", "a")})) == []
    assert decide(snap([], running_jobs={job_key(K.PLAN_TASKS, "tm")})) == []
    assert decide(snap(sw=swarm(dod=None), running_jobs={job_key(K.BOOTSTRAP)})) == []
    assert decide(snap(sw=swarm("LEARNING"), running_jobs={job_key(K.RETROSPECTIVE)})) == []


def test_action_job_key_matches_helper():
    a = Action(kind=K.WORK_TASK, team_id="tm", task_id="w")
    assert a.job_key() == job_key(K.WORK_TASK, "tm", "w")
