"""DAASH's agent-facing API (docs/DESIGN-0.4.md section 6): a second listener that serves only
`GET /health`, the DAASH MCP server (`/mcp`) and the runner protocol (`/runner/*`), so they can
be published to other machines without publishing the admin API.

Run with ``uvicorn app.agent_api:app --port 8001`` (compose service `agent-api`, entrypoint mode
`agent-api`). The coordinator (app.main) owns migrations; this app only opens the database.
Every route here authenticates with its own tokens (runner tokens, MCP tokens); nothing here
reaches an admin route, and there are no API docs pages.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import asyncpg
from fastapi import FastAPI

from app import mcp_server, runner_api
from app.api_state import ModuleState
from app.bus import DecisionBus
from app.config import Settings
from app.db import create_pool
from app.embeddings import build_embedder

log = logging.getLogger(__name__)

settings = Settings()
pool: Optional[asyncpg.Pool] = None
bus: Optional[DecisionBus] = None
embedder: Any = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, bus, embedder
    pool = await create_pool(settings.DATABASE_URL)
    bus = DecisionBus(settings.REDIS_URL, ttl_seconds=settings.BUS_TTL_SECONDS,
                      signals_max=settings.BUS_SIGNALS_MAX)
    embedder = build_embedder(settings.EMBEDDING_MODEL, settings.EMBEDDING_USE_MODEL)
    log.info("Agent API ready (MCP at %s, runner protocol at /runner/*).", mcp_server.MCP_PATH)
    try:
        yield
    finally:
        await bus.close()
        await pool.close()
        pool = None


app = FastAPI(title="DAASH agent API", lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url=None)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "agent-api"}


_state = ModuleState(__name__)
app.include_router(runner_api.build_router(_state))
app.include_router(mcp_server.build_router(_state))
