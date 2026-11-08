from __future__ import annotations

from pydantic_settings import BaseSettings


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

    # Artifacts produced by agents live under WORKSPACE_ROOT/<swarm_id>/.
    WORKSPACE_ROOT: str = "/workspaces"

    # Engine (python -m app.engine).
    ENGINE_TICK_SECONDS: float = 3.0
    ENGINE_ENABLED: bool = True

    model_config = {"env_prefix": "DAASH_"}
