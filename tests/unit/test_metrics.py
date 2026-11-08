import json
from datetime import datetime, timedelta, timezone

import pytest

from app.metrics import compute_metrics
from app.models import AgentCall, Task, TaskEvent, Team

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


def task(id, status="Pending", team="t1", **kw):
    return Task(id=id, swarm_id="s", team_id=team, name=f"N{id}", status=status,
                created_at=NOW, updated_at=NOW, **kw)


def done(id, secs, done_at, team="t1"):
    return task(id, "Done", team, committed_at=done_at - timedelta(seconds=secs), done_at=done_at)


def ev(kind, at, team="t1", id=1):
    return TaskEvent(id=id, swarm_id="s", team_id=team, kind=kind, created_at=at)


def call(at, ok=True, tokens=100, model="fast", role="mission_delivery", id="c"):
    return AgentCall(id=id, swarm_id="s", role=role, model=model, purpose="work_task",
                     total_tokens=tokens, ok=ok, created_at=at)


def team(id="t1", name="Alpha"):
    return Team(id=id, swarm_id="s", name=name, mission_statement="m", created_at=NOW)


def test_empty_inputs():
    m = compute_metrics([], [], [], [], NOW)
    assert m["p90_cycle_seconds"] is None
    assert m["cycle_times"] == []
    assert m["team_failure_rates"] == []
    assert m["tokens"] == {"total": 0, "by_model": {}, "by_role": {}}
    assert m["cost_usd"] == 0.0
    assert len(m["error_rate"]["series"]) == 24
    assert m["error_rate"]["interval_seconds"] == 3600
    assert all(b["errors"] == 0 and b["calls"] == 0 and b["rate"] == 0.0
               for b in m["error_rate"]["series"])
    assert set(m["tasks_by_status"].values()) == {0}
    json.dumps(m)


def test_p90_and_cycle_times():
    tasks = [done(f"d{i}", i * 10, NOW - H * (30 - i)) for i in range(1, 26)]
    m = compute_metrics(tasks, [], [], [], NOW)
    assert len(m["cycle_times"]) == 20
    assert m["cycle_times"][-1]["task_id"] == "d25"            # chronological, newest last
    assert m["cycle_times"][0]["task_id"] == "d6"
    assert m["cycle_times"][-1]["seconds"] == 250.0
    assert m["p90_cycle_seconds"] == 230.0                      # ceil(22.5)=23rd of 10..250


def test_p90_with_single_done_task_and_missing_timestamps():
    tasks = [done("a", 60, NOW), task("b", "Done"), task("c", "Done", done_at=NOW)]
    m = compute_metrics(tasks, [], [], [], NOW)
    assert m["p90_cycle_seconds"] == 60.0
    assert [c["task_id"] for c in m["cycle_times"]] == ["a"]
    assert m["tasks_by_status"]["Done"] == 3


def test_tasks_by_status_zero_filled():
    m = compute_metrics([task("a", "Ready"), task("b", "Ready"), task("c", "Failed")],
                        [], [], [], NOW)
    s = m["tasks_by_status"]
    assert s["Ready"] == 2 and s["Failed"] == 1 and s["Review"] == 0 and s["Rejected"] == 0


def test_error_buckets():
    calls = [call(NOW - timedelta(minutes=10), ok=False, id="1"),       # last bucket
             call(NOW - timedelta(minutes=20), ok=True, id="2"),        # last bucket
             call(NOW - H * 2 - timedelta(minutes=5), ok=True, id="3"),  # bucket 21
             call(NOW - H * 30, ok=False, id="4")]                       # outside window
    events = [ev("rework", NOW - timedelta(minutes=5)),
              ev("error", NOW - timedelta(minutes=6), id=2),
              ev("done", NOW - timedelta(minutes=7), id=3),              # not an error
              ev("failed", NOW - H * 100, id=4)]                         # outside
    m = compute_metrics([], events, calls, [], NOW)
    series = m["error_rate"]["series"]
    last = series[-1]
    assert last["calls"] == 2 and last["errors"] == 3 and last["rate"] == 1.5
    assert series[21]["calls"] == 1 and series[21]["errors"] == 0 and series[21]["rate"] == 0.0
    assert sum(b["calls"] for b in series) == 3
    assert series[-1]["start"] == (NOW - H).isoformat()
    assert series[0]["start"] == (NOW - H * 24).isoformat()


def test_bucket_boundaries():
    at_now = call(NOW, id="1")                         # included in last bucket
    at_start = call(NOW - H * 24, id="2")              # first bucket
    just_before = call(NOW - H * 24 - timedelta(seconds=1), id="3")   # excluded
    future = call(NOW + timedelta(seconds=1), id="4")                 # excluded
    boundary = call(NOW - H, id="5")                   # start of last bucket
    m = compute_metrics([], [], [at_now, at_start, just_before, future, boundary], [], NOW)
    s = m["error_rate"]["series"]
    assert s[-1]["calls"] == 2 and s[0]["calls"] == 1
    assert sum(b["calls"] for b in s) == 3


def test_custom_interval_and_buckets():
    m = compute_metrics([], [], [call(NOW - timedelta(seconds=30))], [], NOW,
                        interval_seconds=60, buckets=3)
    assert m["error_rate"]["interval_seconds"] == 60
    assert [b["calls"] for b in m["error_rate"]["series"]] == [0, 0, 1]


def test_naive_datetimes_treated_as_utc():
    naive_call = call(datetime(2026, 3, 1, 11, 30), id="n")
    m = compute_metrics([], [], [naive_call], [], NOW)
    assert m["error_rate"]["series"][-1]["calls"] == 1


def test_team_failure_rates():
    tasks = [task("a", "Done", "t1", committed_at=NOW), task("b", "Failed", "t1", committed_at=NOW),
             task("c", "Pending", "t1"), task("d", "Done", "t2", committed_at=NOW)]
    events = [ev("rework", NOW, "t1", 1), ev("rework", NOW, "t1", 2), ev("failed", NOW, "t1", 3),
              ev("error", NOW, "t1", 4), ev("rework", NOW, "t2", 5), ev("rework", NOW, None, 6)]
    m = compute_metrics(tasks, events, [], [team("t1"), team("t2", "Beta"), team("t3", "Idle")], NOW)
    rows = {r["team_id"]: r for r in m["team_failure_rates"]}
    assert rows["t1"] == {"team_id": "t1", "team_name": "Alpha", "tasks_committed": 2,
                          "failed_attempts": 3, "tasks_failed": 1, "failures_per_task": 1.5}
    assert rows["t2"]["failures_per_task"] == 1.0
    assert rows["t3"]["tasks_committed"] == 0 and rows["t3"]["failures_per_task"] == 0.0


def test_tokens_and_cost():
    calls = [call(NOW, tokens=1000, model="fast", role="mission_delivery", id="1"),
             call(NOW, tokens=500, model="great", role="quality_judgement", id="2"),
             call(NOW, tokens=1500, model="fast", role="quality_judgement", id="3")]
    m = compute_metrics([], [], calls, [], NOW, prices={"fast": 2.0, "great": 4.0})
    assert m["tokens"]["total"] == 3000
    assert m["tokens"]["by_model"] == {"fast": 2500, "great": 500}
    assert m["tokens"]["by_role"] == {"mission_delivery": 1000, "quality_judgement": 2000}
    assert m["cost_usd"] == pytest.approx(5.0 + 2.0)
    assert compute_metrics([], [], calls, [], NOW)["cost_usd"] == 0.0


def test_tokens_count_even_outside_bucket_window():
    m = compute_metrics([], [], [call(NOW - H * 500, tokens=7)], [], NOW)
    assert m["tokens"]["total"] == 7


def test_json_serialisable_with_data():
    m = compute_metrics([done("a", 5, NOW)], [ev("rework", NOW)], [call(NOW, ok=False)],
                        [team()], NOW, prices={"fast": 1.0})
    assert json.loads(json.dumps(m)) == m
