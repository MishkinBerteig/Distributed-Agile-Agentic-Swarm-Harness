"""Tests for the harness adapter layer (Slice 3)."""
from __future__ import annotations

import httpx

import pytest
import pytest_asyncio

from app.adapter import (
    AgentMessage,
    AgentTurn,
    CompositeAdapter,
    HarnessAdapterError,
    LMStudioAdapter,
    build_adapter,
    get_active_adapter,
)


# ---------------------------------------------------------------------------
# LMStudioAdapter
# ---------------------------------------------------------------------------

def _make_fake_response(text: str = "I can help with that.",
                        model: str = "qwen3.8-27b-mlx",
                        finish_reason: str = "stop",
                        usage: dict | None = None) -> dict:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "model": model,
        "usage": usage or {},
    }


@pytest.mark.asyncio
async def test_lmstudio_execute_ok():
    """Happy path: adapter returns text, model, finish_reason, usage."""
    def handler(_req):
        return httpx.Response(200, json=_make_fake_response())

    adapter = LMStudioAdapter(
        endpoint="http://127.0.0.1:12345",
        model="qwen3.8-27b-mlx",
        transport=httpx.MockTransport(handler),
    )

    result = await adapter.execute(
        AgentTurn(
            system_prompt="You are helpful.",
            messages=[AgentMessage(role="user", content="Hello")],
            max_tokens=1024,
            temperature=0.5,
        )
    )

    assert result.text == "I can help with that."
    assert result.model == "qwen3.8-27b-mlx"
    assert result.finish_reason == "stop"


@pytest.mark.asyncio
async def test_lmstudio_usage_included():
    """Usage dict from the response is passed through."""
    def handler(_req):
        return httpx.Response(200, json=_make_fake_response(usage={"prompt_tokens": 50, "completion_tokens": 20}))

    adapter = LMStudioAdapter(transport=httpx.MockTransport(handler))

    result = await adapter.execute(
        AgentTurn(
            system_prompt="Test.",
            messages=[AgentMessage(role="user", content="hi")],
        )
    )

    assert result.usage == {"prompt_tokens": 50, "completion_tokens": 20}


@pytest.mark.asyncio
async def test_lmstudio_no_api_key_header():
    """When api_key is None/empty, no Authorization header is sent."""
    captured_headers: dict[str, str] = {}

    def handler(req):
        captured_headers.update({k: v for k, v in req.headers.items() if k.lower() in ("authorization", "content-type")})
        return httpx.Response(200, json=_make_fake_response())

    adapter = LMStudioAdapter(
        endpoint="http://127.0.0.1:12345",
        model="qwen3.8-27b-mlx",
        transport=httpx.MockTransport(handler),
    )

    await adapter.execute(
        AgentTurn(system_prompt="Test.", messages=[AgentMessage(role="user", content="hi")])
    )

    assert "authorization" not in captured_headers


@pytest.mark.asyncio
async def test_lmstudio_with_api_key():
    """When api_key is provided, Authorization header is sent."""
    captured_headers: dict[str, str] = {}

    def handler(req):
        captured_headers.update({k: v for k, v in req.headers.items() if k.lower() in ("authorization", "content-type")})
        return httpx.Response(200, json=_make_fake_response())

    adapter = LMStudioAdapter(
        endpoint="http://127.0.0.1:12345",
        model="qwen3.8-27b-mlx",
        api_key="sk-secret",
        transport=httpx.MockTransport(handler),
    )

    await adapter.execute(
        AgentTurn(system_prompt="Test.", messages=[AgentMessage(role="user", content="hi")])
    )

    assert captured_headers.get("authorization") == "Bearer sk-secret"


@pytest.mark.asyncio
async def test_lmstudio_http_error():
    """Non-200 status raises HarnessAdapterError."""
    def handler(_req):
        return httpx.Response(500, text="Internal Error")

    adapter = LMStudioAdapter(transport=httpx.MockTransport(handler))

    with pytest.raises(HarnessAdapterError, match="500"):
        await adapter.execute(
            AgentTurn(system_prompt="x", messages=[AgentMessage(role="user", content="hi")])
        )


@pytest.mark.asyncio
async def test_lmstudio_parse_error():
    """Malformed response raises HarnessAdapterError."""
    def handler(_req):
        return httpx.Response(200, json={"data": "no choices here"})

    adapter = LMStudioAdapter(transport=httpx.MockTransport(handler))

    with pytest.raises(HarnessAdapterError, match="parse_error|Unexpected"):
        await adapter.execute(
            AgentTurn(system_prompt="x", messages=[AgentMessage(role="user", content="hi")])
        )


# ---------------------------------------------------------------------------
# CompositeAdapter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_composite_default():
    """Composite routes to default adapter when harness=_default."""
    def handler(_req):
        return httpx.Response(200, json=_make_fake_response(text="yes", model="test-model"))

    default = LMStudioAdapter(transport=httpx.MockTransport(handler))
    composite = CompositeAdapter(default)

    result = await composite.execute(
        AgentTurn(
            system_prompt="Default route.",
            messages=[AgentMessage(role="user", content="what?")],
            metadata={"harness": "_default"},
        )
    )

    assert result.text == "yes"


@pytest.mark.asyncio
async def test_composite_unknown_harness():
    """Unknown harness raises HarnessAdapterError."""
    composite = CompositeAdapter(LMStudioAdapter())
    with pytest.raises(HarnessAdapterError, match="Unknown harness"):
        await composite.execute(
            AgentTurn(
                system_prompt="x",
                messages=[AgentMessage(role="user", content="x")],
                metadata={"harness": "nonexistent"},
            )
        )


# ---------------------------------------------------------------------------
# build_adapter / get_active_adapter
# ---------------------------------------------------------------------------

class FakeSettings:
    LLM_ENDPOINT = "http://127.0.0.1:12345"
    LLM_MODEL = "qwen3.8-27b-mlx"
    LLM_API_KEY = ""


def test_build_and_get():
    composite = build_adapter(FakeSettings())
    active = get_active_adapter()
    assert isinstance(composite, CompositeAdapter)
    assert active is composite

    # Reset global for other tests
    import app.adapter
    app.adapter._active_adapter = None
