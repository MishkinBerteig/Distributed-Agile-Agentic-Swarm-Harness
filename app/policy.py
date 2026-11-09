"""Pure domain policy for DAASH 0.1 (docs/DESIGN-0.1.md section 6).

No I/O. Lifecycle and task transitions, the Definition of Ready gate, the
Definition of Done evaluator, p90 cycle time, voting and resource limits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.models import (
    SWARM_ACTIONS,
    Agent,
    AgentRole,
    AgentState,
    DoDThresholds,
    Swarm,
    Task,
    TaskStatus,
    TeamStatus,
)

# ---------------------------------------------------------------------------
# Swarm lifecycle
# ---------------------------------------------------------------------------


def transition(state: str, action: str) -> str:
    """Apply a lifecycle action to a swarm state; ValueError if not allowed."""
    state = getattr(state, "value", state)
    moves = SWARM_ACTIONS.get(action)
    if moves is None:
        raise ValueError(
            f"Unknown swarm action '{action}'. Valid actions: {', '.join(sorted(SWARM_ACTIONS))}.")
    if state not in moves:
        allowed = allowed_actions(state)
        hint = (f"From {state} you can: {', '.join(allowed)}." if allowed
                else f"{state} is a terminal state.")
        raise ValueError(f"Cannot '{action}' a swarm in state {state}. {hint}")
    return moves[state]


def allowed_actions(state: str) -> list[str]:
    """Lifecycle actions legal from `state`, in SWARM_ACTIONS order."""
    state = getattr(state, "value", state)
    return [a for a, moves in SWARM_ACTIONS.items() if state in moves]


# ---------------------------------------------------------------------------
# Task status transitions (DESIGN 6.1)
# ---------------------------------------------------------------------------

_S = TaskStatus
TASK_TRANSITIONS: dict[str, set[str]] = {
    _S.PENDING.value: {_S.READY.value, _S.REJECTED.value},
    _S.READY.value: {_S.IN_PROGRESS.value, _S.PENDING.value, _S.REJECTED.value},
    # In-Progress -> In-Progress is a rework loop; -> Rejected is a team restart.
    _S.IN_PROGRESS.value: {_S.REVIEW.value, _S.IN_PROGRESS.value, _S.FAILED.value,
                           _S.REJECTED.value},
    # Review -> Rejected: a team restart rejects every open task, Review included.
    _S.REVIEW.value: {_S.DONE.value, _S.IN_PROGRESS.value, _S.FAILED.value,
                      _S.REJECTED.value},
    _S.DONE.value: set(),
    _S.FAILED.value: set(),
    _S.REJECTED.value: set(),
}


def can_transition_task(frm: str, to: str) -> bool:
    frm = getattr(frm, "value", frm)
    to = getattr(to, "value", to)
    return to in TASK_TRANSITIONS.get(frm, set())


# ---------------------------------------------------------------------------
# Cycle time
# ---------------------------------------------------------------------------


def p90(values: list[float]) -> Optional[float]:
    """Nearest-rank 90th percentile: the ceil(0.9*n)-th smallest; None if empty."""
    n = len(values)
    if n == 0:
        return None
    rank = (9 * n + 9) // 10  # integer ceil(0.9 * n), no float rounding
    return sorted(values)[rank - 1]


def trailing_p90_cycle_seconds(tasks: list[Task], window: int = 100,
                               min_samples: int = 5) -> Optional[float]:
    """p90 of (done_at - committed_at) over the most recent `window` Done tasks.

    Tasks missing either timestamp are ignored. Fewer than `min_samples`
    usable tasks gives None (no constraint yet).
    """
    done = [t for t in tasks
            if t.status == TaskStatus.DONE.value and t.committed_at and t.done_at]
    done.sort(key=lambda t: t.done_at, reverse=True)
    recent = done[:window]
    if not recent or len(recent) < min_samples:
        return None
    return p90([max(0.0, (t.done_at - t.committed_at).total_seconds()) for t in recent])


# ---------------------------------------------------------------------------
# Task / agent selection helpers
# ---------------------------------------------------------------------------


def deps_done(task: Task, tasks_by_id: dict[str, Task]) -> bool:
    """True when every dependency exists and is Done (unknown ids are not done)."""
    for dep_id in task.depends_on:
        dep = tasks_by_id.get(dep_id)
        if dep is None or dep.status != TaskStatus.DONE.value:
            return False
    return True


def stalled_task_ids(tasks: list[Task]) -> set[str]:
    """Pending/Ready tasks that can never start: a dependency is missing, Failed or
    Rejected, or is itself stalled (transitive)."""
    by_id = {t.id: t for t in tasks}
    waiting = (TaskStatus.PENDING.value, TaskStatus.READY.value)
    dead = (TaskStatus.FAILED.value, TaskStatus.REJECTED.value)
    stalled: set[str] = set()
    changed = True
    while changed:
        changed = False
        for t in tasks:
            if t.id in stalled or t.status not in waiting:
                continue
            for d in t.depends_on:
                dep = by_id.get(d)
                if dep is None or dep.status in dead or dep.id in stalled:
                    stalled.add(t.id)
                    changed = True
                    break
    return stalled


def live_open_tasks(tasks: list[Task]) -> list[Task]:
    """Open tasks that can still make progress (stalled ones do not hold a team back)."""
    stalled = stalled_task_ids(tasks)
    return [t for t in tasks
            if t.status in TaskStatus.open_statuses() and t.id not in stalled]


def task_budget_used(tasks: list[Task]) -> int:
    """Tasks that count against `max_tasks_per_team`: all of them except tasks rejected before
    anyone committed to them (Mission Judgement turned the idea down; no work was spent)."""
    return sum(1 for t in tasks if not _rejected_unstarted(t))


def task_budget_left(tasks: list[Task], limit: int) -> int:
    """Room for new tasks. Rejected-before-start tasks are free, but only up to `limit` of them,
    so a team whose plans keep being rejected still runs out (runaway guard)."""
    rejected = sum(1 for t in tasks if _rejected_unstarted(t))
    if rejected >= limit:
        return 0
    return max(0, limit - task_budget_used(tasks))


def _rejected_unstarted(t: Task) -> bool:
    return t.status == TaskStatus.REJECTED.value and t.committed_at is None


def work_in_flight(team_id: str, teams: list, tasks: list[Task]) -> bool:
    """Can anything in the swarm still change while `team_id`'s candidates wait (deferral)?

    True when some non-stalled task anywhere is Ready, In-Progress or Review (this team's
    included), or another ACTIVE team has no standards yet or no live open task (it will plan or
    be assessed next). Teams that only hold Pending tasks cannot unblock each other by waiting."""
    stalled = stalled_task_ids(tasks)
    moving = (TaskStatus.READY.value, TaskStatus.IN_PROGRESS.value, TaskStatus.REVIEW.value)
    if any(t.status in moving and t.id not in stalled for t in tasks):
        return True
    for tm in teams:
        if tm.id == team_id or tm.status != TeamStatus.ACTIVE.value:
            continue
        if tm.definition_of_done is None:
            return True
        if not live_open_tasks([t for t in tasks if t.team_id == tm.id]):
            return True
    return False


def idle_delivery_agents(team_agents: list[Agent], team_tasks: list[Task]) -> list[Agent]:
    """Delivery agents that are idle and own no In-Progress/Review task."""
    busy_ids = {t.assigned_agent_id for t in team_tasks
                if t.assigned_agent_id
                and t.status in (TaskStatus.IN_PROGRESS.value, TaskStatus.REVIEW.value)}
    return [a for a in team_agents
            if a.role == AgentRole.MISSION_DELIVERY.value
            and a.state == AgentState.IDLE.value
            and a.id not in busy_ids]


def overdue_in_progress(team_tasks: list[Task], p90_seconds: Optional[float],
                        now: datetime) -> list[Task]:
    """In-Progress tasks committed longer ago than the p90 cycle time."""
    if p90_seconds is None:
        return []
    return [t for t in team_tasks
            if t.status == TaskStatus.IN_PROGRESS.value and t.committed_at is not None
            and (now - t.committed_at).total_seconds() > p90_seconds]


def next_ready_task(team_tasks: list[Task]) -> Optional[Task]:
    """Highest-priority Ready task whose dependencies are all Done.

    Lowest `priority` first (None last); ties by approved_at (None last), then
    created_at.
    """
    by_id = {t.id: t for t in team_tasks}
    candidates = [t for t in team_tasks
                  if t.status == TaskStatus.READY.value and deps_done(t, by_id)]
    if not candidates:
        return None
    return min(candidates, key=lambda t: (
        t.priority is None, t.priority if t.priority is not None else 0,
        t.approved_at is None, t.approved_at,
        t.created_at))


# ---------------------------------------------------------------------------
# Definition of Ready (DESIGN 6.2)
# ---------------------------------------------------------------------------


@dataclass
class DoRResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def _fmt_secs(s: float) -> str:
    s = int(round(s))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s / 60:.1f} min"
    return f"{s / 3600:.1f} h"


def dor_check(task: Task, team_tasks: list[Task], team_agents: list[Agent],
              p90_seconds: Optional[float], now: datetime) -> DoRResult:
    """All four commitment-policy rules; `reasons` lists every rule that fails."""
    reasons: list[str] = []

    # Rule 1: a free Mission Delivery agent.
    if not idle_delivery_agents(team_agents, team_tasks):
        reasons.append("No Mission Delivery agent on this team is idle and free of "
                       "In-Progress work, so nobody can take the task.")

    # Rule 2: nothing in the team is older than the trailing p90 cycle time.
    for t in overdue_in_progress(team_tasks, p90_seconds, now):
        age = _fmt_secs((now - t.committed_at).total_seconds())
        reasons.append(
            f"Task '{t.name}' has been In-Progress for {age}, longer than the trailing "
            f"p90 cycle time of {_fmt_secs(p90_seconds)}; finish or unblock it before "
            f"committing new work.")

    # Rule 3: approved by Mission Judgement and the next highest priority.
    pool = list(team_tasks)
    if all(t.id != task.id for t in pool):
        pool.append(task)
    by_id = {t.id: t for t in pool}
    if task.status != TaskStatus.READY.value:
        reasons.append(f"Task '{task.name}' is {task.status}, not Ready: Mission "
                       f"Judgement has not approved it.")
    else:
        nxt = next_ready_task(pool)
        if nxt is None or nxt.id != task.id:
            other = f" ('{nxt.name}')" if nxt else ""
            reasons.append(f"Task '{task.name}' is not the next highest-priority Ready "
                           f"task{other} with all dependencies Done.")

    # Rule 4: acceptance criteria and finished dependencies.
    if not task.criteria:
        reasons.append(f"Task '{task.name}' has no acceptance criteria; add at least one.")
    if not deps_done(task, by_id):
        missing = [d for d in task.depends_on
                   if d not in by_id or by_id[d].status != TaskStatus.DONE.value]
        reasons.append(f"Task '{task.name}' depends on tasks that are not Done: "
                       f"{', '.join(missing)}.")

    return DoRResult(ok=not reasons, reasons=reasons)


# ---------------------------------------------------------------------------
# Definition of Done (DESIGN 6.3)
# ---------------------------------------------------------------------------


@dataclass
class DoDResult:
    passed: bool
    quality_passed: bool
    mission_passed: bool
    quality_score: float
    reasons: list[str] = field(default_factory=list)


def evaluate_dod(deterministic: list[bool], rubric_scores: list[float], qj_passed: bool,
                 mj_met: bool, mj_confidence: float,
                 thresholds: DoDThresholds) -> DoDResult:
    """Combine deterministic checks, rubric scores and both judges' verdicts.

    Empty deterministic or rubric lists pass vacuously.
    """
    reasons: list[str] = []

    det_mean = (sum(1 for d in deterministic if d) / len(deterministic)
                if deterministic else 1.0)
    det_ok = det_mean >= thresholds.deterministic
    if not det_ok:
        failed = sum(1 for d in deterministic if not d)
        reasons.append(
            f"{failed} of {len(deterministic)} deterministic checks failed "
            f"({det_mean:.1%} passing; {thresholds.deterministic:.1%} required).")

    rubric_ok = True
    for i, s in enumerate(rubric_scores, start=1):
        if s < thresholds.rubric:
            rubric_ok = False
            reasons.append(f"Rubric criterion {i} scored {s:.3f}, below the required "
                           f"{thresholds.rubric:.3f}.")

    if not qj_passed:
        reasons.append("Quality Judgement did not pass the deliverable overall.")

    quality_passed = det_ok and rubric_ok and qj_passed

    scores = [1.0 if d else 0.0 for d in deterministic] + [float(s) for s in rubric_scores]
    quality_score = sum(scores) / len(scores) if scores else (1.0 if qj_passed else 0.0)

    mission_passed = bool(mj_met) and mj_confidence >= thresholds.mission_confidence
    if not mj_met:
        reasons.append("Mission Judgement says the expected value is not met.")
    elif mj_confidence < thresholds.mission_confidence:
        reasons.append(f"Mission Judgement confidence {mj_confidence:.3f} is below the "
                       f"required {thresholds.mission_confidence:.3f}.")

    return DoDResult(passed=quality_passed and mission_passed,
                     quality_passed=quality_passed, mission_passed=mission_passed,
                     quality_score=quality_score, reasons=reasons)


def rework_or_fail(attempts_after_increment: int, max_attempts: int) -> str:
    """'failed' once attempts reach the cap, otherwise 'rework'."""
    return "failed" if attempts_after_increment >= max_attempts else "rework"


# ---------------------------------------------------------------------------
# Voting (DESIGN 6.4)
# ---------------------------------------------------------------------------


def majority_quorum(n: int) -> int:
    return n // 2 + 1


def tally(votes: dict[str, bool], n_voters: int) -> str:
    """'approved' | 'rejected' | 'pending'.

    Approved once approvals reach the majority quorum; rejected once the
    rejections leave too few possible approvals. With no voters nothing can
    be approved, so the result is 'rejected'.
    """
    quorum = majority_quorum(n_voters)
    approvals = sum(1 for v in votes.values() if v)
    rejections = len(votes) - approvals
    if approvals >= quorum:
        return "approved"
    if n_voters - rejections < quorum:
        return "rejected"
    return "pending"


# ---------------------------------------------------------------------------
# Limits and cost (DESIGN 6.5)
# ---------------------------------------------------------------------------


def cost_usd(tokens_by_model: dict[str, int], prices_per_1k: dict[str, float]) -> float:
    """USD cost; models without a price are free."""
    return sum(tokens / 1000.0 * prices_per_1k.get(model, 0.0)
               for model, tokens in tokens_by_model.items())


def limits_exceeded(swarm: Swarm, tokens_used: int, cost_usd: float,
                    now: datetime) -> list[str]:
    """Reasons (empty if none) a swarm has hit a resource limit.

    A limit counts as exceeded once usage reaches it (>=). None limits are off.
    """
    lim = swarm.config.limits
    reasons: list[str] = []
    if lim.max_wall_clock_minutes is not None and swarm.started_at is not None:
        elapsed = (now - swarm.started_at).total_seconds() / 60.0
        if elapsed >= lim.max_wall_clock_minutes:
            reasons.append(
                f"Wall-clock limit reached: {elapsed:.1f} of {lim.max_wall_clock_minutes} "
                f"minutes used. Raise limits.max_wall_clock_minutes to continue.")
    if lim.max_total_tokens is not None and tokens_used >= lim.max_total_tokens:
        reasons.append(
            f"Token limit reached: {tokens_used:,} of {lim.max_total_tokens:,} tokens "
            f"used. Raise limits.max_total_tokens to continue.")
    if lim.max_budget_usd is not None and cost_usd >= lim.max_budget_usd:
        reasons.append(
            f"Budget limit reached: ${cost_usd:.2f} of ${lim.max_budget_usd:.2f} spent. "
            f"Raise limits.max_budget_usd to continue.")
    return reasons
