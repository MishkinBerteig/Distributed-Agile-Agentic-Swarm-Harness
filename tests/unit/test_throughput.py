"""0.2 throughput and convergence: buffer, slots, per-model concurrency, ref counts, cancellation."""
import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.engine import Engine, decide, keys_to_cancel
from app.jobs import Jobs, RefCounts, SwarmNotActive, prioritize_slots
from app.llm import LLMClient
from app.models import ActionKind as K
from app.prompts import parse_prioritize, prioritize_prompt
from tests.unit.test_decide import agent, kinds, snap, task
from tests.unit.test_prompts import task as ptask


# -- decide: buffer = number of delivery agents ---------------------------------

def two_busy():
    return [agent("d1", state="busy"), agent("d2", state="busy")]


def test_buffer_two_agents_prioritizes_with_one_ready():
    tasks = [task("r1", "Ready"), task("p1"), task("p2")]
    assert K.PRIORITIZE in kinds(decide(snap(tasks, agents=two_busy())))


def test_buffer_two_agents_full_does_not_prioritize():
    tasks = [task("r1", "Ready"), task("r2", "Ready"), task("p1")]
    assert K.PRIORITIZE not in kinds(decide(snap(tasks, agents=two_busy())))


def test_single_agent_pipelining_unchanged():
    busy = [agent("d1", state="busy")]
    assert K.PRIORITIZE in kinds(decide(snap([task("p1")], agents=busy)))
    assert K.PRIORITIZE not in kinds(decide(snap([task("r1", "Ready"), task("p1")], agents=busy)))


def test_ready_with_unfinished_deps_does_not_fill_buffer():
    tasks = [task("x", "In-Progress", assigned_agent_id="d1"),
             task("r1", "Ready", depends_on=["x"]), task("p1")]
    assert K.PRIORITIZE in kinds(decide(snap(tasks, agents=[agent("d1", state="busy")])))


def test_no_prioritize_without_candidates():
    assert K.PRIORITIZE not in kinds(decide(snap([task("r1", "Ready")], agents=two_busy())))


def test_resume_redispatches_in_progress_and_review():
    tasks = [task("w", "In-Progress", assigned_agent_id="d1"), task("v", "Review")]
    out = [(a.kind, a.task_id) for a in decide(snap(tasks))]
    assert (K.WORK_TASK, "w") in out and (K.REVIEW_TASK, "v") in out


def test_prioritize_slots():
    assert prioritize_slots(2, 0) == 2
    assert prioritize_slots(3, 1) == 2
    assert prioritize_slots(1, 1) == 1
    assert prioritize_slots(0, 0) == 1


# -- parse_prioritize / prompt ---------------------------------------------------

IDS = ["t1", "t2", "t3"]


def test_parse_slots_list_capped():
    r = parse_prioritize({"order": ["t3", "t1", "t2"], "next_task_ids": ["t3", "t1", "t2"]},
                         IDS, slots=2)
    assert r.next_task_ids == ["t3", "t1"] and r.next_task_id == "t3"


def test_parse_dedupes_and_drops_rejected():
    r = parse_prioritize({"order": IDS, "next_task_ids": ["t1", "t1", "t2", "t3"],
                          "rejections": [{"task_id": "t1", "reason": "x"}]}, IDS, slots=3)
    assert r.next_task_ids == ["t2", "t3"]


def test_parse_legacy_single_id():
    r = parse_prioritize({"order": IDS, "next_task_id": "t2"}, IDS)
    assert r.next_task_ids == ["t2"] and r.next_task_id == "t2"


def test_parse_both_fields_dedupe():
    r = parse_prioritize({"order": IDS, "next_task_ids": ["t2"], "next_task_id": "t2"}, IDS,
                         slots=2)
    assert r.next_task_ids == ["t2"]


def test_parse_missing_fields_fall_back_to_first_non_rejected_in_order():
    r = parse_prioritize({"order": ["t2", "t1", "t3"],
                          "rejections": [{"task_id": "t2", "reason": "x"}]}, IDS, slots=2)
    assert r.next_task_ids == ["t1", "t3"] and not r.deferred


def test_parse_explicit_empty_list_defers_instead_of_approving():
    r = parse_prioritize({"order": ["t2", "t1", "t3"], "next_task_ids": [], "next_task_id": None,
                          "rejections": [{"task_id": "t2", "reason": "x"}]}, IDS, slots=2)
    assert r.next_task_ids == [] and r.next_task_id is None and r.deferred
    assert r.rejections == [("t2", "x")]


def test_parse_empty_list_with_everything_rejected_is_not_a_deferral():
    r = parse_prioritize({"order": ["t1"], "next_task_ids": [],
                          "rejections": [{"task_id": "t1", "reason": "x"}]}, ["t1"], slots=1)
    assert r.next_task_ids == [] and not r.deferred


def test_parse_a_listed_task_is_approved_even_when_next_task_id_is_null():
    r = parse_prioritize({"order": IDS, "next_task_ids": ["t1"], "next_task_id": None}, IDS)
    assert r.next_task_ids == ["t1"] and not r.deferred


def test_prompt_lets_mission_judgement_defer_and_shows_workspace_files():
    p = prioritize_prompt([ptask("t1")], [], slots=1, workspace_files="calendar.md (10 bytes)")
    assert '"next_task_ids": []' in p
    assert "Never reject a task only because it cannot start yet" in p
    assert "calendar.md (10 bytes)" in p
    assert "(none yet)" in prioritize_prompt([ptask("t1")], [], slots=1)


def test_parse_all_rejected_is_empty():
    r = parse_prioritize({"order": ["t1"], "rejections": [{"task_id": "t1", "reason": "x"}]},
                         ["t1"], slots=2)
    assert r.next_task_ids == [] and r.next_task_id is None


def test_parse_unknown_id_in_list_raises():
    with pytest.raises(ValueError):
        parse_prioritize({"order": IDS, "next_task_ids": ["zz"]}, IDS, slots=2)


def test_prompt_mentions_slots():
    p = prioritize_prompt([ptask("t1")], [], slots=3)
    assert "up to 3" in p and "next_task_ids" in p
    assert "up to 1" in prioritize_prompt([ptask("t1")], [])


# -- LLM per-model concurrency -----------------------------------------------------

MSGS = [{"role": "user", "content": "x"}]


def chat_body():
    return {"model": "m", "choices": [{"message": {"role": "assistant", "content": "ok"},
                                       "finish_reason": "stop"}], "usage": {}}


def tracking_client(**kw):
    state = {"now": {}, "peak": {}}

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        state["now"][model] = state["now"].get(model, 0) + 1
        state["peak"][model] = max(state["peak"].get(model, 0), state["now"][model])
        for _ in range(5):  # yield to the loop so other calls can pile up
            await asyncio.sleep(0)
        state["now"][model] -= 1
        return httpx.Response(200, json=chat_body())

    async def no_sleep(_):
        return None

    c = LLMClient("http://gw:1", transport=httpx.MockTransport(handler), sleep=no_sleep, **kw)
    return c, state


def test_concurrency_default_limit():
    async def go():
        c, st = tracking_client(default_concurrency=2)
        await asyncio.gather(*[c.chat("a", MSGS) for _ in range(7)])
        return st["peak"]
    assert asyncio.run(go()) == {"a": 2}


def test_concurrency_model_override():
    async def go():
        c, st = tracking_client(default_concurrency=3, model_concurrency={"slow": 1})
        await asyncio.gather(*[c.chat("slow", MSGS) for _ in range(5)],
                             *[c.chat("fast", MSGS) for _ in range(8)])
        return st["peak"]
    peak = asyncio.run(go())
    assert peak["slow"] == 1 and peak["fast"] == 3


def test_concurrency_unlimited_by_default():
    async def go():
        c, st = tracking_client()
        await asyncio.gather(*[c.chat("a", MSGS) for _ in range(6)])
        return st["peak"]
    assert asyncio.run(go())["a"] == 6


def test_semaphore_not_held_during_retry_backoff():
    seen_free = []
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(500 if calls["n"] == 1 else 200, json=chat_body())

    async def sleep(_):
        seen_free.append(not c._sems["a"].locked())

    c = LLMClient("http://gw:1", transport=httpx.MockTransport(handler), sleep=sleep,
                  default_concurrency=1)
    asyncio.run(c.chat("a", MSGS))
    assert calls["n"] == 2 and seen_free == [True]


# -- settings --------------------------------------------------------------------------

def test_settings_model_concurrency_parsing(caplog):
    assert Settings(LLM_MODEL_CONCURRENCY='{"amazing": 1, "fast": "3"}').model_concurrency() == {
        "amazing": 1, "fast": 3}
    assert Settings().model_concurrency() == {} and Settings().LLM_MAX_CONCURRENCY == 4
    with caplog.at_level("WARNING"):
        assert Settings(LLM_MODEL_CONCURRENCY="{bad").model_concurrency() == {}
        assert Settings(LLM_MODEL_CONCURRENCY="[1]").model_concurrency() == {}
    assert "ignoring bad" in caplog.text


# -- agent ref counts / busy ------------------------------------------------------------

def test_refcounts():
    r = RefCounts()
    assert r.enter("a") is True and r.enter("a") is False and r.count("a") == 2
    assert r.exit("a") is False and r.exit("a") is True and r.count("a") == 0
    assert r.exit("a") is True  # underflow is harmless


class FakeAgents:
    def __init__(self):
        self.log = []

    async def set_state(self, agent_id, state, task_id=None):
        self.log.append((agent_id, state))


def fake_jobs():
    j = Jobs.__new__(Jobs)
    j._locks, j._refs, j.agents = {}, RefCounts(), FakeAgents()
    return j


def test_judges_run_concurrently_and_idle_only_at_zero():
    async def go():
        j = fake_jobs()
        mj = agent("mj", role="mission_judgement")
        inside, gate = [], asyncio.Event()

        async def job(n):
            async with j.busy(mj):
                inside.append(n)
                await gate.wait()

        ts = [asyncio.create_task(job(i)) for i in range(3)]
        for _ in range(10):
            await asyncio.sleep(0)
        assert inside == [0, 1, 2]  # not serialized
        assert j._refs.count("mj") == 3 and j.agents.log == [("mj", "busy")]
        gate.set()
        await asyncio.gather(*ts)
        return j
    j = asyncio.run(go())
    assert j.agents.log == [("mj", "busy"), ("mj", "idle")] and j._refs.count("mj") == 0


def test_delivery_agent_jobs_are_serialized():
    async def go():
        j = fake_jobs()
        d = agent("d1")
        active, peak = [0], [0]

        async def job():
            async with j.busy(d):
                active[0] += 1
                peak[0] = max(peak[0], active[0])
                await asyncio.sleep(0)
                active[0] -= 1

        await asyncio.gather(*[job() for _ in range(3)])
        return peak[0], j._refs.count("d1")
    assert asyncio.run(go()) == (1, 0)


def test_busy_decrements_on_exception_and_cancellation():
    async def go():
        j = fake_jobs()
        mj = agent("mj", role="mission_judgement")
        with pytest.raises(RuntimeError):
            async with j.busy(mj):
                raise RuntimeError("boom")
        assert j._refs.count("mj") == 0

        async def hang():
            async with j.busy(mj):
                await asyncio.sleep(3600)

        t = asyncio.create_task(hang())
        for _ in range(5):
            await asyncio.sleep(0)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        return j
    j = asyncio.run(go())
    assert j._refs.count("mj") == 0 and j.agents.log[-1] == ("mj", "idle")


# -- cancel on leaving ACTIVE ---------------------------------------------------------------

KEYS = ["work_task:t1:k1", "review_task:t1:k2", "retrospective:-:-", "prioritize:t1:-"]


def test_keys_to_cancel():
    assert keys_to_cancel(KEYS, "ACTIVE") == []
    assert keys_to_cancel(KEYS, "LEARNING") == [k for k in KEYS if not k.startswith("retro")]
    for st in ("PAUSED", "VERIFICATION", "ARCHIVED", "USER_FEEDBACK", "CREATED", None):
        assert keys_to_cancel(KEYS, st) == KEYS


def test_engine_cancel_stale_cancels_and_clears_keys():
    async def go():
        eng = Engine(Settings())
        started = []

        async def job(k):
            started.append(k)
            await asyncio.sleep(3600)

        for k in ("work_task:t:1", "retrospective:-:-"):
            eng.running[k] = asyncio.create_task(job(k))
        await asyncio.sleep(0)
        await eng._cancel_stale("LEARNING")
        left = set(eng.running)
        await eng._cancel_stale("PAUSED")
        return left, set(eng.running), started
    left, after, started = asyncio.run(go())
    assert left == {"retrospective:-:-"} and after == set() and len(started) == 2


def test_swarm_not_active_is_not_cancellation():
    assert not issubclass(SwarmNotActive, asyncio.CancelledError)


def test_parse_without_allow_defer_falls_back_to_approving():
    r = parse_prioritize({"order": ["t2", "t1"], "next_task_ids": [], "next_task_id": None},
                         ["t1", "t2"], slots=1, allow_defer=False)
    assert r.next_task_ids == ["t2"] and not r.deferred


def test_prompt_without_can_wait_offers_no_deferral():
    p = prioritize_prompt([ptask("t1")], [], slots=1, can_wait=False)
    assert "waiting cannot change anything" in p
    assert '"next_task_ids": []' not in p and "Never reject a task only because" not in p
