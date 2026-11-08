from datetime import datetime, timezone

import pytest

from app import repo
from app.models import AcceptanceCriterion, Swarm, SwarmConfig, Task, TeamProposal

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_vec_literal():
    assert repo._vec_literal([1, 0.5, -2.25]) == "[1.0,0.5,-2.25]"
    assert repo._vec_or_none(None) is None
    assert repo._vec_or_none([0.0]) == "[0.0]"


def test_build_update_numbers_params_and_rejects_unknown():
    sets, params = repo.build_update(repo.TASK_UPDATABLE, {"status": "Done", "attempts": 2})
    assert sets == "status = $2, attempts = $3"
    assert params == ["Done", 2]
    with pytest.raises(ValueError):
        repo.build_update(repo.TASK_UPDATABLE, {"status": "Done", "id = 'x'; --": 1})
    with pytest.raises(ValueError):
        repo.build_update(repo.TASK_UPDATABLE, {"embedding": [1]})
    with pytest.raises(ValueError):
        repo.build_update(repo.TEAM_UPDATABLE, {})


def test_whitelists_exclude_identity_and_embedding():
    for wl in (repo.SWARM_UPDATABLE, repo.TEAM_UPDATABLE, repo.TASK_UPDATABLE):
        assert "id" not in wl and "embedding" not in wl and "swarm_id" not in wl


def test_column_lists_never_select_embedding():
    for cols in (repo.TASK_COLS, repo.LEARNING_COLS, repo.MEMORY_COLS):
        assert "embedding" not in cols


def test_compute_cost():
    assert repo.compute_cost({"a": 2000, "b": 1000}, {"a": 0.5}) == pytest.approx(1.0)
    assert repo.compute_cost({}, {"a": 1}) == 0.0
    assert repo.compute_cost({"free": 10**6}, {}) == 0.0


def test_row_to_model_mapping():
    sw = repo._to(Swarm, {"id": "s", "name": "n", "vision_statement": "v",
                          "config": {"delivery_agents_per_team": 2}, "created_at": NOW,
                          "embedding": "ignored"})
    assert isinstance(sw.config, SwarmConfig) and sw.config.delivery_agents_per_team == 2
    crit = AcceptanceCriterion(id="c", task_id="t", ordinal=1, text="x")
    t = repo._to(Task, {"id": "t", "swarm_id": "s", "team_id": "tm", "name": "n",
                        "depends_on": ["a"], "deliverable": {"summary": "ok"},
                        "created_at": NOW, "updated_at": NOW}, criteria=[crit])
    assert t.depends_on == ["a"] and t.criteria[0].id == "c"
    p = repo._to(TeamProposal, {"id": "p", "swarm_id": "s", "team_id": "t", "proposer_agent_id": "a",
                                "name": "n", "mission_statement": "m", "votes": {"a": True},
                                "created_at": NOW})
    assert p.votes == {"a": True}
    assert repo._to(Swarm, None) is None


def test_dump_config_and_enum_unwrap():
    d = repo._dump_config(SwarmConfig())
    assert isinstance(d, dict) and d["limits"]["max_teams"] == 4
    assert repo._dump_config({"x": 1}) == {"x": 1}
    from app.models import TaskStatus
    assert repo._val(TaskStatus.DONE) == "Done" and repo._val("x") == "x"
