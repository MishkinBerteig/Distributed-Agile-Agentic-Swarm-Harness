from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
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

    # LLM harness (Slice 3: Harness Adapters). Defaults to local LMStudio.
    LLM_ENDPOINT: str = "http://127.0.0.1:12345"
    LLM_MODEL: str = "qwen3.8-27b-mlx"
    LLM_API_KEY: str = ""  # LMStudio by default requires no key

    model_config = {"env_prefix": "DAASH_"}
