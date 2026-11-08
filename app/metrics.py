"""Dashboard metrics (docs/DESIGN-0.1.md section 6.7). Pure; output is JSON-serialisable."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models import AgentCall, Task, TaskEvent, TaskStatus, Team
from app.policy import cost_usd as _cost_usd
from app.policy import trailing_p90_cycle_seconds

ERROR_EVENT_KINDS = frozenset({"rework", "failed", "error"})
FAILED_ATTEMPT_KINDS = frozenset({"rework", "failed"})
CYCLE_TIME_LIMIT = 20


def _utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC so they compare with aware ones."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _bucket_index(ts: datetime, first_start: datetime, interval: timedelta,
                  buckets: int, now: datetime) -> Optional[int]:
    """Bucket for `ts`, or None when outside the window. `now` falls in the last bucket."""
    ts = _utc(ts)
    if ts < first_start or ts > now:
        return None
    idx = int((ts - first_start) / interval)
    return min(idx, buckets - 1)


def compute_metrics(tasks: list[Task], events: list[TaskEvent], calls: list[AgentCall],
                    teams: list[Team], now: datetime, interval_seconds: int = 3600,
                    buckets: int = 24, prices: Optional[dict[str, float]] = None) -> dict:
    now = _utc(now)
    interval_seconds = max(1, int(interval_seconds))
    buckets = max(1, int(buckets))

    # Cycle times: p90 over the last 100 Done tasks (any sample count), last 20 listed.
    p90_cycle = trailing_p90_cycle_seconds(tasks, window=100, min_samples=1)
    done = sorted((t for t in tasks
                   if t.status == TaskStatus.DONE.value and t.committed_at and t.done_at),
                  key=lambda t: _utc(t.done_at))
    cycle_times = [{"task_id": t.id, "name": t.name,
                    "seconds": max(0.0, (t.done_at - t.committed_at).total_seconds())}
                   for t in done[-CYCLE_TIME_LIMIT:]]

    # Error-rate series: `buckets` windows ending at `now`, oldest first.
    interval = timedelta(seconds=interval_seconds)
    first_start = now - interval * buckets
    series = [{"start": (first_start + interval * i).isoformat(),
               "errors": 0, "calls": 0, "rate": 0.0} for i in range(buckets)]
    for c in calls:
        i = _bucket_index(c.created_at, first_start, interval, buckets, now)
        if i is not None:
            series[i]["calls"] += 1
            if not c.ok:
                series[i]["errors"] += 1
    for e in events:
        if e.kind in ERROR_EVENT_KINDS:
            i = _bucket_index(e.created_at, first_start, interval, buckets, now)
            if i is not None:
                series[i]["errors"] += 1
    for b in series:
        b["rate"] = b["errors"] / b["calls"] if b["calls"] else 0.0

    # Per-team failure rates.
    team_rows = []
    for team in teams:
        committed = sum(1 for t in tasks if t.team_id == team.id and t.committed_at is not None)
        failed_attempts = sum(1 for e in events
                              if e.team_id == team.id and e.kind in FAILED_ATTEMPT_KINDS)
        tasks_failed = sum(1 for t in tasks
                           if t.team_id == team.id and t.status == TaskStatus.FAILED.value)
        team_rows.append({
            "team_id": team.id, "team_name": team.name,
            "tasks_committed": committed, "failed_attempts": failed_attempts,
            "tasks_failed": tasks_failed,
            "failures_per_task": failed_attempts / committed if committed else 0.0,
        })

    by_status = {s.value: 0 for s in TaskStatus}
    for t in tasks:
        by_status[t.status] = by_status.get(t.status, 0) + 1

    by_model: dict[str, int] = {}
    by_role: dict[str, int] = {}
    total = 0
    for c in calls:
        total += c.total_tokens
        by_model[c.model] = by_model.get(c.model, 0) + c.total_tokens
        by_role[c.role] = by_role.get(c.role, 0) + c.total_tokens

    return {
        "p90_cycle_seconds": p90_cycle,
        "cycle_times": cycle_times,
        "error_rate": {"interval_seconds": interval_seconds, "series": series},
        "team_failure_rates": team_rows,
        "tasks_by_status": by_status,
        "tokens": {"total": total, "by_model": by_model, "by_role": by_role},
        "cost_usd": _cost_usd(by_model, prices or {}),
    }
