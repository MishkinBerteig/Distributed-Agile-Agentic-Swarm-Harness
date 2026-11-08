"""LLM client, tool loop and JSON extraction (app/llm.py) against httpx.MockTransport."""
from __future__ import annotations

import json

import httpx
import pytest

from app.llm import LLMClient, LLMError, extract_json
from app.tools import Tool, ToolRegistry


def resp(content=None, tool_calls=None, finish="stop", reasoning=None, usage=(10, 5)):
    msg = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": "fast-x", "choices": [{"message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                      "total_tokens": usage[0] + usage[1]}}


def tc(name, args, id="c1"):
    return {"id": id, "type": "function",
            "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


class Script:
    """Replays scripted (status, json) responses and records requests."""

    def __init__(self, *items):
        self.items = list(items)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item if isinstance(item, tuple) else (200, item)
        return httpx.Response(status, json=body)

    def bodies(self):
        return [json.loads(r.content) for r in self.requests if r.content]


def client(script, **kw):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    c = LLMClient("http://gw:1/", "KEY", transport=httpx.MockTransport(script),
                  sleep=fake_sleep, **kw)
    c.sleeps = sleeps
    return c


def submit_registry():
    got = {}

    async def submit(args):
        got.update(args)
        return "submitted"

    async def echo(args):
        return "echo:" + str(args.get("x"))

    reg = ToolRegistry([Tool("echo", "e", {"type": "object"}, echo),
                        Tool("submit", "s", {"type": "object"}, submit, terminal=True)])
    return reg, got


# ----------------------------------------------------------------- extract_json

def test_extract_json_variants():
    assert extract_json('<think>{"no": 1}</think>\n```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert extract_json('Sure! Here: {"a": "}{"} thanks') == {"a": "}{"}
    assert extract_json("ok [1, {\"b\": 2}] end") == [1, {"b": 2}]
    assert extract_json('think</think>{"x": 1}') == {"x": 1}
    assert extract_json('text {broken} then {"ok": true}') == {"ok": True}


@pytest.mark.parametrize("text", ["", "no json here", "<think>{\"a\":1}", "{unclosed"])
def test_extract_json_raises(text):
    with pytest.raises(ValueError):
        extract_json(text)


# ----------------------------------------------------------------- chat / models

async def test_list_models_and_headers():
    s = Script({"data": [{"id": "fast"}, {"id": "great"}]})
    c = client(s)
    assert await c.list_models() == ["fast", "great"]
    assert s.requests[0].url.path == "/v1/models"
    assert s.requests[0].headers["authorization"] == "Bearer KEY"


async def test_chat_reasoning_only_response():
    s = Script(resp(content=None, reasoning="thinking...", finish="length"))
    r = await client(s).chat("fast", [{"role": "user", "content": "hi"}])
    assert r.content == "" and r.reasoning == "thinking..." and r.finish_reason == "length"
    assert r.usage.total_tokens == 15 and r.model == "fast-x"
    assert r.message == {"role": "assistant", "content": ""}


async def test_chat_no_content_key_and_null():
    s = Script({"choices": [{"message": {"content": None}, "finish_reason": "stop"}]})
    r = await client(s).chat("fast", [])
    assert r.content == "" and r.usage.total_tokens == 0


async def test_retry_503_then_success():
    s = Script((503, {"error": "busy"}), (429, {}), resp("hello"))
    c = client(s)
    r = await c.chat("fast", [])
    assert r.content == "hello" and len(s.requests) == 3 and c.sleeps == [1.0, 2.0]


async def test_retries_exhausted_and_connection_errors():
    s = Script((500, {}), (500, {}), (500, {}))
    with pytest.raises(LLMError) as ei:
        await client(s).chat("fast", [])
    assert ei.value.code == "http" and ei.value.status == 500
    s = Script(httpx.ConnectError("x"), httpx.ConnectError("x"), httpx.ConnectError("x"))
    with pytest.raises(LLMError) as ei:
        await client(s).chat("fast", [])
    assert ei.value.code == "connection"
    s = Script(httpx.ReadTimeout("t"), resp("late"))
    assert (await client(s).chat("fast", [])).content == "late"
    s = Script(httpx.ReadTimeout("t"), httpx.ReadTimeout("t"), httpx.ReadTimeout("t"))
    with pytest.raises(LLMError) as ei:
        await client(s).chat("fast", [])
    assert ei.value.code == "timeout"


async def test_no_retry_on_400():
    s = Script((400, {"error": "bad"}), resp("never"))
    c = client(s)
    with pytest.raises(LLMError) as ei:
        await c.chat("fast", [])
    assert ei.value.code == "http" and len(s.requests) == 1 and c.sleeps == []


async def test_protocol_errors():
    for body in ({"choices": []}, {"nothing": 1}):
        with pytest.raises(LLMError) as ei:
            await client(Script(body)).chat("fast", [])
        assert ei.value.code == "protocol"


async def test_tool_call_fallback_tag():
    content = 'Let me.<tool_call>{"name": "echo", "arguments": {"x": 1}}</tool_call>'
    r = await client(Script(resp(content))).chat("fast", [])
    assert [(t.name, json.loads(t.arguments)) for t in r.tool_calls] == [("echo", {"x": 1})]
    assert r.content == "Let me."
    assert r.message["tool_calls"][0]["function"]["name"] == "echo"


# ----------------------------------------------------------------- tool loop

async def test_tool_loop_terminal():
    s = Script(resp(tool_calls=[tc("echo", {"x": 1})], finish="tool_calls"),
               resp(tool_calls=[tc("submit", {"summary": "done"}, "c2")], finish="tool_calls"))
    reg, got = submit_registry()
    r = await client(s).run_tool_loop("fast", "SYS", "USER", reg, max_steps=5)
    assert r.stopped_by == "terminal" and r.terminal_tool == "submit"
    assert r.terminal_output == "submitted" and got == {"summary": "done"}
    assert r.steps == 2 and r.usage.total_tokens == 30
    roles = [e["role"] for e in r.transcript]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    second = s.bodies()[1]["messages"]
    assert second[-1] == {"role": "tool", "tool_call_id": "c1", "content": "echo:1"}
    assert s.bodies()[0]["tools"][0]["type"] == "function"


async def test_tool_loop_nudge_then_terminal():
    s = Script(resp("all done!"), resp(tool_calls=[tc("submit", {"summary": "s"})]))
    reg, _ = submit_registry()
    r = await client(s).run_tool_loop("fast", "S", "U", reg, max_steps=5)
    assert r.stopped_by == "terminal" and r.steps == 2
    nudge = s.bodies()[1]["messages"][-1]
    assert nudge["role"] == "user" and "submit" in nudge["content"]


async def test_tool_loop_nudged_once_then_answer():
    s = Script(resp("a"), resp("still no tool"))
    reg, _ = submit_registry()
    r = await client(s).run_tool_loop("fast", "S", "U", reg, max_steps=5)
    assert r.stopped_by == "answer" and r.final_text == "still no tool" and r.steps == 2


async def test_tool_loop_no_terminal_required():
    s = Script(resp("just text"))
    reg, _ = submit_registry()
    r = await client(s).run_tool_loop("fast", "S", "U", reg, 5, require_terminal=False)
    assert r.stopped_by == "answer" and r.final_text == "just text" and len(s.requests) == 1


async def test_tool_loop_max_steps():
    s = Script(*[resp(tool_calls=[tc("echo", {"x": i}, f"c{i}")]) for i in range(3)])
    reg, _ = submit_registry()
    r = await client(s).run_tool_loop("fast", "S", "U", reg, max_steps=3)
    assert r.stopped_by == "max_steps" and r.steps == 3 and r.terminal_tool is None


async def test_tool_loop_invalid_args_returns_error_to_model():
    s = Script(resp(tool_calls=[tc("echo", "{not json")]),
               resp(tool_calls=[tc("submit", {"summary": "ok"}, "c2")]))
    reg, _ = submit_registry()
    r = await client(s).run_tool_loop("fast", "S", "U", reg, 5)
    assert r.stopped_by == "terminal"
    assert "not valid JSON" in s.bodies()[1]["messages"][-1]["content"]


async def test_tool_loop_tag_fallback_and_truncated_transcript():
    big = "z" * 10_000
    s = Script(resp(f'<tool_call>{{"name": "echo", "arguments": {{"x": "{big}"}}}}</tool_call>'),
               resp(tool_calls=[tc("submit", {})]))
    reg, _ = submit_registry()
    r = await client(s).run_tool_loop("fast", "S", "U", reg, 5)
    assert r.stopped_by == "terminal"
    tool_entry = [e for e in r.transcript if e["role"] == "tool"][0]
    assert len(tool_entry["content"]) < 4100 and "truncated" in tool_entry["content"]


async def test_tool_loop_error_carries_usage():
    s = Script(resp(tool_calls=[tc("echo", {"x": 1})]), (400, {"e": 1}))
    reg, _ = submit_registry()
    with pytest.raises(LLMError) as ei:
        await client(s).run_tool_loop("fast", "S", "U", reg, 5)
    assert ei.value.usage.total_tokens == 15 and len(ei.value.transcript) >= 4


# ----------------------------------------------------------------- complete_json

def parse_int_a(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get("a"), int):
        raise ValueError('"a" must be an integer')
    return obj["a"]


async def test_complete_json_ok_and_think_stripped():
    s = Script(resp('<think>hmm</think>```json\n{"a": 3}\n```'))
    val, res = await client(s).complete_json("fast", "S", "U", parse_int_a)
    assert val == 3 and res.steps == 1 and res.usage.total_tokens == 15


async def test_complete_json_reasks_with_error():
    s = Script(resp('{"a": "x"}'), resp('{"a": 7}'))
    val, res = await client(s).complete_json("fast", "S", "U", parse_int_a)
    assert val == 7 and res.steps == 2 and res.usage.total_tokens == 30
    last = s.bodies()[1]["messages"][-1]["content"]
    assert '"a" must be an integer' in last
    assert s.bodies()[1]["messages"][-2]["role"] == "assistant"


async def test_complete_json_fails_after_retry():
    s = Script(resp("nope"), resp("still nope"))
    with pytest.raises(LLMError) as ei:
        await client(s).complete_json("fast", "S", "U", parse_int_a)
    e = ei.value
    assert e.code == "parse" and e.usage.total_tokens == 30 and len(e.transcript) >= 4


async def test_complete_json_length_with_empty_content():
    s = Script(resp(None, reasoning="long thoughts", finish="length"))
    with pytest.raises(LLMError) as ei:
        await client(s).complete_json("fast", "S", "U", parse_int_a)
    assert ei.value.code == "protocol" and "max_tokens" in str(ei.value).lower() or "MAX_TOKENS" in str(ei.value)
    assert ei.value.usage.total_tokens == 15 and len(s.requests) == 1


async def test_complete_json_http_error_has_usage():
    s = Script(resp("bad"), (400, {}))
    with pytest.raises(LLMError) as ei:
        await client(s).complete_json("fast", "S", "U", parse_int_a)
    assert ei.value.code == "http" and ei.value.usage.total_tokens == 15
