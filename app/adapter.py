"""Harness adapters for DAASH agent communication.

Each adapter wraps a specific harness (OpenClaude, Claude Code, Codex, etc.)
and provides a uniform execute() interface that the coordinator calls to
delegate work to agents.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)


@dataclass
class AgentMessage:
    """A single message in a conversation turn."""
    role: str  # "system", "user", "assistant"
    content: str


@dataclass
class AgentTurn:
    """A complete agent execution turn."""
    system_prompt: str
    messages: list[AgentMessage] = field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 0.7
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResponse:
    """Result of an agent turn."""
    text: str
    model: str
    finish_reason: str
    usage: dict[str, int] = field(default_factory=dict)


class HarnessAdapterError(Exception):
    """Raised when a harness adapter encounters an error."""
    def __init__(self, message: str, code: str = "adapter_error"):
        super().__init__(message)
        self.message = message
        self.code = code


class BaseAdapter(ABC):
    """Abstract base class for harness adapters."""

    @abstractmethod
    async def execute(self, turn: AgentTurn) -> AgentResponse:
        """Execute an agent turn and return the response.

        Args:
            turn: The agent turn containing system prompt, messages, and params.

        Returns:
            AgentResponse with the model output.

        Raises:
            HarnessAdapterError: On adapter-specific failures.
        """
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable adapter name."""
        ...


class LMStudioAdapter(BaseAdapter):
    """Adapter for LMStudio-compatible OpenAI-endpoint servers.

    Supports any harness that exposes the OpenAI-compatible chat completions
    API (LMStudio, ollama with openai adapter, vllm, etc.).
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:12345",
        model: str = "qwen3.8-27b-mlx",
        api_key: str = "",
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._api_key = api_key or None
        self._timeout = timeout
        self._transport = transport

    @property
    def name(self) -> str:
        return "lmstudio"

    async def execute(self, turn: AgentTurn) -> AgentResponse:
        url = f"{self._endpoint}/v1/chat/completions"

        # Build message list: system + user/assistant history
        messages = [{"role": "system", "content": turn.system_prompt}]
        for msg in turn.messages:
            messages.append({"role": msg.role, "content": msg.content})

        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": turn.max_tokens,
            "temperature": turn.temperature,
            "stream": False,
        }

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                resp = await client.post(url, json=body, headers=headers)

            if resp.status_code != 200:
                raise HarnessAdapterError(
                    f"LMStudio returned {resp.status_code}: {resp.text}",
                    code="http_error",
                )

            data = resp.json()
            choice = data["choices"][0]
            message = choice["message"]

            return AgentResponse(
                text=message["content"],
                model=data.get("model", self._model),
                finish_reason=choice.get("finish_reason", "stop"),
                usage=data.get("usage", {}),
            )

        except httpx.TimeoutException:
            raise HarnessAdapterError(
                f"Request to {self._endpoint} timed out after {self._timeout}s",
                code="timeout",
            )
        except httpx.ConnectError:
            raise HarnessAdapterError(
                f"Cannot connect to LMStudio at {self._endpoint}. "
                "Is it running and a model loaded?",
                code="connection_error",
            )
        except (KeyError, IndexError) as exc:
            raise HarnessAdapterError(
                f"Unexpected response format from LMStudio: {exc}",
                code="parse_error",
            )


class CompositeAdapter(BaseAdapter):
    """Routes turns to the appropriate adapter based on metadata.

    Can be extended later to support multi-harness swarms where different
    agents use different underlying harnesses.
    """

    def __init__(self, default: BaseAdapter):
        self._adapters: dict[str, BaseAdapter] = {"_default": default}

    @property
    def name(self) -> str:
        return "composite"

    def register(self, harness_name: str, adapter: BaseAdapter):
        self._adapters[harness_name] = adapter

    async def execute(self, turn: AgentTurn) -> AgentResponse:
        harness = turn.metadata.get("harness", "_default")
        adapter = self._adapters.get(harness)
        if adapter is None:
            raise HarnessAdapterError(
                f"Unknown harness '{harness}'. "
                f"Available: {list(self._adapters.keys())}",
                code="unknown_harness",
            )
        log.info("Routing turn to harness=%s model=%s", harness, self._adapters[harness].name)
        return await adapter.execute(turn)


# ---------------------------------------------------------------------------
# Module-level singleton, initialised from settings
# ---------------------------------------------------------------------------

_active_adapter: CompositeAdapter | None = None


def build_adapter(settings) -> CompositeAdapter:
    """Build the composite adapter from DAASH settings.

    Uses LMStudio as the default harness with configurable endpoint/model.
    """
    global _active_adapter

    default = LMStudioAdapter(
        endpoint=settings.LLM_ENDPOINT,
        model=settings.LLM_MODEL,
        api_key=settings.LLM_API_KEY or "",
    )
    composite = CompositeAdapter(default)
    composite.register("lmstudio", default)
    _active_adapter = composite
    return composite


def get_active_adapter() -> CompositeAdapter:
    if _active_adapter is None:
        raise HarnessAdapterError("Adapter not initialised. Call build_adapter(settings) first.")
    return _active_adapter
