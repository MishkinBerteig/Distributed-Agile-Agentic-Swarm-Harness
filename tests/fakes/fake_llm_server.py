"""The fake LLM as an HTTP server (docs/DESIGN-0.2.md A.1), for the e2e tier.

    python -m tests.fakes.fake_llm_server --port 9100

OpenAI-compatible:  GET /v1/models, POST /v1/chat/completions
Control (JSON):     POST /control/scenario   body = scenario keys (see Scenario), merged
                    GET  /control/scenario   current scenario
                    GET  /control/calls      {"calls": [...], "count": N}; ?since=<seq> filters
                    POST /control/reset      happy path, forget calls
                    GET  /health
"""
from __future__ import annotations

import argparse
from typing import Optional

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from tests.fakes.fake_llm import FakeLLM


def create_app(fake: Optional[FakeLLM] = None) -> FastAPI:
    fake = fake or FakeLLM()
    app = FastAPI(title="DAASH fake LLM")
    app.state.fake = fake

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models() -> JSONResponse:
        status, data = fake.models_response()
        return JSONResponse(data, status_code=status)

    @app.post("/v1/chat/completions")
    async def chat(body: dict = Body(...)) -> JSONResponse:
        status, data = await fake.ahandle(body)
        return JSONResponse(data, status_code=status)

    @app.get("/control/scenario")
    async def get_scenario() -> dict:
        return fake.scenario_dict()

    @app.post("/control/scenario")
    async def set_scenario(body: Optional[dict] = Body(default=None)) -> dict:
        try:
            return fake.configure(body or {})
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))

    @app.get("/control/calls")
    async def get_calls(since: int = 0) -> dict:
        calls = [c for c in fake.calls if c["seq"] > since]
        return {"calls": calls, "count": len(fake.calls)}

    @app.post("/control/reset")
    async def reset() -> dict:
        fake.reset()
        return {"status": "reset"}

    return app


app = create_app()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9100)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
