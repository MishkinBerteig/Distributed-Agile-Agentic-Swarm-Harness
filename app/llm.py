"""Native "daash" harness: OpenAI-compatible chat client, tool loop, JSON helpers.

See docs/DESIGN-0.1.md section 8. The gateway serves reasoning models, so
`message.content` may be missing/None while `reasoning_content` is present.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from app.tools import ToolRegistry

T = TypeVar("T")

TRANSCRIPT_CHARS = 4000
NUDGE = ("You have not finished. Call the `{tool}` tool now to finish "
         "(do not just reply with text).")


class LLMError(Exception):
    """LLM call failed. code: connection|timeout|http|parse|protocol.

    `usage` and `transcript` carry what was spent before failing so callers can
    still account for tokens."""

    def __init__(self, message: str, code: str = "protocol", *, usage: "Usage | None" = None,
                 transcript: list[dict] | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.usage = usage or Usage()
        self.transcript = transcript or []
        self.status = status


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ChatResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    duration_ms: int = 0
    message: dict = field(default_factory=dict)


@dataclass
class LoopResult:
    final_text: str = ""
    steps: int = 0
    usage: Usage = field(default_factory=Usage)
    transcript: list[dict] = field(default_factory=list)
    terminal_tool: str | None = None
    terminal_output: str | None = None
    stopped_by: str = "answer"  # terminal | answer | max_steps


# ---------------------------------------------------------------- JSON helpers

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)```", re.DOTALL)


def _balanced_spans(text: str):
    """Yield (start, end) of balanced {...}/[...] spans, string-aware, in order of start."""
    i, n = 0, len(text)
    while i < n:
        if text[i] in "{[":
            depth, in_str, esc = 0, False, False
            for j in range(i, n):
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                    if depth == 0:
                        yield i, j + 1
                        break
        i += 1


def _first_json(text: str) -> Any:
    for s, e in _balanced_spans(text):
        try:
            return json.loads(text[s:e])
        except ValueError:
            continue
    raise ValueError("no JSON found")


def extract_json(text: str) -> Any:
    """Return the first balanced JSON object/array in `text`, ignoring <think>, fences, prose."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty reply: no JSON found")
    cleaned = _THINK.sub("", text)
    if "</think>" in cleaned.lower():  # opening tag was cut off
        cleaned = re.split(r"</think>", cleaned, flags=re.IGNORECASE)[-1]
    elif "<think>" in cleaned.lower():  # never closed: only thinking
        cleaned = re.split(r"<think>", cleaned, flags=re.IGNORECASE)[0]
    for m in _FENCE.finditer(cleaned):
        try:
            return _first_json(m.group(1))
        except ValueError:
            continue
    try:
        return _first_json(cleaned)
    except ValueError:
        raise ValueError(f"no JSON object or array found in reply: {cleaned.strip()[:200]!r}") from None


# ---------------------------------------------------------------- helpers

_TOOL_TAG = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def _truncate(s: Any, limit: int = TRANSCRIPT_CHARS) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= limit else s[:limit] + f"...[truncated {len(s) - limit} chars]"


def _transcript_entry(msg: dict, reasoning: str = "") -> dict:
    e: dict[str, Any] = {"role": msg.get("role"), "content": _truncate(msg.get("content"))}
    if reasoning:
        e["reasoning"] = _truncate(reasoning)
    if msg.get("tool_calls"):
        e["tool_calls"] = [{"name": c["function"]["name"],
                            "arguments": _truncate(c["function"]["arguments"], 1000)}
                           for c in msg["tool_calls"]]
    if msg.get("tool_call_id"):
        e["tool_call_id"] = msg["tool_call_id"]
    return e


def _usage_of(data: dict) -> Usage:
    u = data.get("usage") or {}
    p, c = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    return Usage(p, c, int(u.get("total_tokens") or (p + c)))


def _parse_tag_calls(content: str) -> tuple[list[ToolCall], str]:
    calls: list[ToolCall] = []
    for m in _TOOL_TAG.finditer(content):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            args = obj.get("arguments", obj.get("parameters", {}))
            calls.append(ToolCall(f"call_{uuid.uuid4().hex[:8]}", obj["name"],
                                  args if isinstance(args, str) else json.dumps(args)))
    return calls, _TOOL_TAG.sub("", content).strip()


# ---------------------------------------------------------------- client

class LLMClient:
    def __init__(self, endpoint: str, api_key: str = "", timeout: float = 600.0,
                 max_tokens: int = 16384, transport: httpx.AsyncBaseTransport | None = None,
                 max_retries: int = 2,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.transport = transport
        self.max_retries = max_retries
        self._sleep = sleep

    def _url(self, path: str) -> str:
        base = self.endpoint[:-3] if self.endpoint.endswith("/v1") else self.endpoint
        return f"{base}/v1/{path}"

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        last: LLMError | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                await self._sleep(float(2 ** (attempt - 1)))
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as c:
                    r = await c.request(method, self._url(path), headers=self._headers(),
                                        json=payload)
            except httpx.TimeoutException as exc:
                last = LLMError(f"LLM request timed out: {exc!r}", "timeout")
                continue
            except httpx.TransportError as exc:
                last = LLMError(f"LLM connection failed: {exc!r}", "connection")
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = LLMError(f"LLM HTTP {r.status_code}: {r.text[:200]}", "http",
                                status=r.status_code)
                continue
            if r.status_code >= 400:
                raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:300]}", "http",
                               status=r.status_code)
            try:
                data = r.json()
            except ValueError:
                raise LLMError(f"LLM returned non-JSON body: {r.text[:200]!r}", "protocol") from None
            if not isinstance(data, dict):
                raise LLMError("LLM returned an unexpected body", "protocol")
            return data
        assert last is not None
        raise last

    async def list_models(self) -> list[str]:
        data = await self._request("GET", "models")
        items = data.get("data")
        if not isinstance(items, list):
            raise LLMError("LLM /models response has no 'data' list", "protocol")
        return [str(m["id"]) for m in items if isinstance(m, dict) and "id" in m]

    async def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                   temperature: float = 0.3, max_tokens: int | None = None) -> ChatResult:
        payload: dict[str, Any] = {"model": model, "messages": messages,
                                   "temperature": temperature,
                                   "max_tokens": max_tokens or self.max_tokens}
        if tools:
            payload["tools"] = tools
        t0 = time.monotonic()
        data = await self._request("POST", "chat/completions", payload)
        dur = int((time.monotonic() - t0) * 1000)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMError("LLM response has no choices", "protocol", usage=_usage_of(data))
        choice = choices[0]
        msg = choice.get("message") or {}
        content = msg.get("content")
        content = content if isinstance(content, str) else ""
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args)
            calls.append(ToolCall(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                  str(fn.get("name", "")), args))
        if not calls and "<tool_call>" in content:
            calls, content = _parse_tag_calls(content)
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": c.arguments}}
                                     for c in calls]
        return ChatResult(content=content, reasoning=reasoning if isinstance(reasoning, str) else "",
                          tool_calls=calls, finish_reason=str(choice.get("finish_reason") or ""),
                          usage=_usage_of(data), model=str(data.get("model") or model),
                          duration_ms=dur, message=message)

    async def run_tool_loop(self, model: str, system: str, user: str, registry: ToolRegistry,
                            max_steps: int, require_terminal: bool = True) -> LoopResult:
        messages: list[dict] = [{"role": "system", "content": system},
                                {"role": "user", "content": user}]
        res = LoopResult(transcript=[_transcript_entry(m) for m in messages])
        specs = registry.specs()
        nudged = False
        res.stopped_by = "max_steps"
        while res.steps < max_steps:
            try:
                r = await self.chat(model, messages, tools=specs or None)
            except LLMError as exc:
                exc.usage.add(res.usage)
                exc.transcript = res.transcript
                raise
            res.steps += 1
            res.usage.add(r.usage)
            messages.append(r.message)
            res.transcript.append(_transcript_entry(r.message, r.reasoning))
            if not r.tool_calls:
                res.final_text = r.content
                if require_terminal and registry.has_terminal() and not nudged:
                    nudged = True
                    term = registry.terminal_name()
                    nudge = {"role": "user", "content": NUDGE.format(tool=term)}
                    messages.append(nudge)
                    res.transcript.append(_transcript_entry(nudge))
                    continue
                res.stopped_by = "answer"
                return res
            terminal = False
            for c in r.tool_calls:
                out = await registry.call(c.name, c.arguments)
                tm = {"role": "tool", "tool_call_id": c.id, "content": out.output}
                messages.append(tm)
                res.transcript.append(_transcript_entry(tm))
                if out.terminal and not terminal:
                    terminal = True
                    res.terminal_tool, res.terminal_output = c.name, out.output
            if r.content:
                res.final_text = r.content
            if terminal:
                res.stopped_by = "terminal"
                return res
        return res

    async def complete_json(self, model: str, system: str, user: str,
                            parse: Callable[[Any], T], retries: int = 1) -> tuple[T, LoopResult]:
        messages: list[dict] = [{"role": "system", "content": system},
                                {"role": "user", "content": user}]
        res = LoopResult(transcript=[_transcript_entry(m) for m in messages])
        last_err = "no reply"
        for attempt in range(retries + 1):
            try:
                r = await self.chat(model, messages)
            except LLMError as exc:
                exc.usage.add(res.usage)
                exc.transcript = res.transcript
                raise
            res.steps += 1
            res.usage.add(r.usage)
            res.transcript.append(_transcript_entry(r.message, r.reasoning))
            res.final_text = r.content
            if r.finish_reason == "length" and not r.content.strip():
                raise LLMError(
                    "The model ran out of output tokens (finish_reason=length) while reasoning "
                    "and produced no answer; raise DAASH_LLM_MAX_TOKENS or use a simpler prompt.",
                    "protocol", usage=res.usage, transcript=res.transcript)
            try:
                return parse(extract_json(r.content)), res
            except ValueError as exc:
                last_err = str(exc)
                if attempt >= retries:
                    break
                messages.append(r.message)
                fix = {"role": "user", "content":
                       f"Your reply could not be used: {last_err}\n"
                       "Reply again with ONLY the corrected JSON, following the contract exactly."}
                messages.append(fix)
                res.transcript.append(_transcript_entry(fix))
        raise LLMError(f"could not parse a valid reply from the model: {last_err}", "parse",
                       usage=res.usage, transcript=res.transcript)
