from __future__ import annotations

import json
import logging

from pydantic_settings import BaseSettings

log = logging.getLogger("daash.config")


class Settings(BaseSettings):
    """Process settings from the environment (prefix ``DAASH_``). See docs/DESIGN-0.1.md §4."""

    DATABASE_URL: str = "postgresql://daash:daash@localhost/daash"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Decision bus (Redis). Signals & proposals are ephemeral: keys expire,
    # PostgreSQL stays the durable source of record.
    BUS_TTL_SECONDS: int = 3600
    BUS_SIGNALS_MAX: int = 200

    EMBEDDING_MODEL: str = "Alibaba-NLP/gte-base-en-v1.5"
    # When false (or when sentence-transformers/model is unavailable), a
    # deterministic hash-based embedder is used instead of downloading the model.
    EMBEDDING_USE_MODEL: bool = False

    # LLM gateway: any OpenAI-compatible endpoint. The default is the m3u512gb
    # private gateway (aliases instant/fast/great/amazing). Its Tailscale name is
    # used because `m3u512gb.local` is mDNS and does not resolve inside Docker.
    LLM_ENDPOINT: str = "http://m3u512gb.tail391a32.ts.net:12345"
    LLM_API_KEY: str = ""
    LLM_TIMEOUT_SECONDS: float = 600.0
    # Gateway models are reasoning models: small budgets come back with only
    # reasoning_content and no answer, so keep this generous.
    LLM_MAX_TOKENS: int = 16384
    # Concurrent HTTP calls per model (one LLM server slows badly above about 4), and
    # per-model overrides as a JSON object, e.g. {"amazing": 1}.
    LLM_MAX_CONCURRENCY: int = 4
    LLM_MODEL_CONCURRENCY: str = "{}"

    # Artifacts produced by agents live under WORKSPACE_ROOT/<swarm_id>/.
    WORKSPACE_ROOT: str = "/workspaces"

    # Engine (python -m app.engine).
    ENGINE_TICK_SECONDS: float = 3.0
    ENGINE_ENABLED: bool = True

    # Authentication (docs/DESIGN-0.3.md section 4). API_TOKEN: bearer token for the admin API and
    # DAASHboard; empty = open (local prototype). RUNNER_BOOTSTRAP_TOKEN: if set, the runner
    # "local-runner" is ensured at start with this token (lets compose start a runner unattended).
    API_TOKEN: str = ""
    RUNNER_BOOTSTRAP_TOKEN: str = ""

    def model_concurrency(self) -> dict[str, int]:
        """LLM_MODEL_CONCURRENCY parsed to {model: limit}; bad JSON is logged and ignored."""
        raw = (self.LLM_MODEL_CONCURRENCY or "").strip()
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                raise ValueError("not a JSON object")
            return {str(k): int(v) for k, v in obj.items() if int(v) >= 1}
        except (ValueError, TypeError) as exc:
            log.warning("ignoring bad DAASH_LLM_MODEL_CONCURRENCY %r: %s", raw, exc)
            return {}

    model_config = {"env_prefix": "DAASH_"}
