"""Runner protocol (docs/DESIGN-0.3.md section 5; runner bearer token) as an APIRouter that
both the admin API (app.main) and the agent-facing API (app.agent_api) mount
(docs/DESIGN-0.4.md section 6).

Since 0.4 a lease also mints the job's DAASH MCP token (section 4.1): the Job JSON carries
`"mcp": {"path": "/mcp", "token": "djt_..."}`, returned only in that lease, its sha256 stored on
the job. The token's tools work while the job is leased (app.mcp_server)."""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import remote as R
from app import services
from app.api_state import require_pool
from app.mcp_server import MCP_PATH, new_job_token
from app.models import RemoteJob, Runner, SwarmStatus
from app.repo import RemoteJobRepo, RunnerRepo, SwarmRepo, TaskRepo

#: Seconds between checks while a /runner/lease long-poll waits (tests shrink it).
LEASE_POLL_SECONDS = 1.0
_WWW_AUTH = {"WWW-Authenticate": "Bearer"}


class HelloRequest(BaseModel):
    host: str = ""
    version: str = ""
    max_jobs: int = Field(1, ge=1, le=64)
    harnesses: list[Any] = Field(default_factory=list)


class LeaseRequest(BaseModel):
    wait_seconds: float = Field(0, ge=0, le=R.MAX_WAIT_SECONDS)
    free_slots: int = Field(1, ge=0, le=64)


class HeartbeatRequest(BaseModel):
    note: Optional[str] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def job_for_runner(job: RemoteJob, mcp_token: Optional[str] = None) -> dict:
    """What the runner receives: the stored payload (without DAASH's own `mcp_*` bookkeeping)
    plus identity fields and, at lease, the job's MCP token."""
    out = {k: v for k, v in job.payload.items() if not k.startswith("mcp_")}
    out.update({"id": job.id, "swarm_id": job.swarm_id, "task_id": job.task_id,
                "attempt": job.attempt, "harness": job.harness, "model": job.model,
                "workspace_url": f"/runner/jobs/{job.id}/workspace"})
    if mcp_token:
        out["mcp"] = {"path": MCP_PATH, "token": mcp_token}
    return out


def build_router(state: Any) -> APIRouter:
    """The /runner/* routes on an app's handles (`state.pool`, `state.settings`)."""
    router = APIRouter()

    def pool():
        return require_pool(state)

    async def runner_auth(request: Request) -> Runner:
        """The enabled runner owning the bearer token, else 401. Every authenticated call counts
        as being seen."""
        token = R.bearer_token(request.headers.get("authorization"))
        runner = await RunnerRepo(pool()).by_token_hash(R.hash_token(token)) if token else None
        if (runner is None or not runner.enabled
                or not R.tokens_match(R.hash_token(token), runner.token_hash)):
            raise HTTPException(401, "Invalid runner token", headers=_WWW_AUTH)
        await RunnerRepo(pool()).touch(runner.id)
        return runner

    @router.post("/runner/hello")
    async def runner_hello(req: HelloRequest, runner: Runner = Depends(runner_auth)) -> dict:
        caps = R.normalize_capabilities(req.harnesses)
        await RunnerRepo(pool()).update(runner.id, host=req.host[:200], version=req.version[:80],
                                        max_jobs=req.max_jobs, harnesses=caps)
        lease_seconds = state.settings.RUNNER_LEASE_SECONDS
        return {"runner_id": runner.id, "name": runner.name, "poll_seconds": R.POLL_SECONDS,
                "heartbeat_seconds": R.heartbeat_seconds_for(lease_seconds),
                "lease_seconds": lease_seconds}

    @router.post("/runner/lease")
    async def runner_lease(req: LeaseRequest, runner: Runner = Depends(runner_auth)):
        """Long-poll for a matching queued job: 200 {"job"} or 204."""
        repo = RemoteJobRepo(pool())
        caps = R.capability_map(runner.harnesses)
        offered = R.harness_offered(runner.harnesses)
        deadline = time.monotonic() + req.wait_seconds
        while req.free_slots > 0:
            now = _now()
            await repo.requeue_expired(now)
            active = (await RunnerRepo(pool()).active_job_counts()).get(runner.id, 0)
            if active < runner.max_jobs:
                token = new_job_token()
                job = await repo.lease_next(runner, caps, now,
                                            state.settings.RUNNER_LEASE_SECONDS,
                                            harness_caps=offered,
                                            mcp_token_hash=R.hash_token(token))
                if job is not None:
                    return {"job": job_for_runner(job, token)}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(LEASE_POLL_SECONDS, remaining))
        return Response(status_code=204)

    async def own_job(job_id: str, runner: Runner) -> RemoteJob:
        """The job if it exists and this runner holds/held it, else 404."""
        job = await RemoteJobRepo(pool()).get(job_id)
        if job is None or job.runner_id != runner.id:
            raise HTTPException(404, "job not found for this runner")
        return job

    @router.get("/runner/jobs/{job_id}/workspace")
    async def runner_workspace(job_id: str, runner: Runner = Depends(runner_auth)) -> Response:
        job = await own_job(job_id, runner)
        if job.status != "leased":
            raise HTTPException(404, "job is not leased by this runner")
        root = services.workspace_dir(state.settings, job.swarm_id)
        data = await asyncio.to_thread(R.build_workspace_tar, root)
        return Response(content=data, media_type="application/gzip")

    async def job_still_wanted(job: RemoteJob) -> bool:
        """False once the swarm left ACTIVE or the task is no longer In-Progress."""
        swarm = await SwarmRepo(pool()).get(job.swarm_id)
        if swarm is None or swarm.lifecycle_state != SwarmStatus.ACTIVE.value:
            return False
        task = await TaskRepo(pool()).get(job.task_id)
        return task is not None and task.status == "In-Progress"

    @router.post("/runner/jobs/{job_id}/heartbeat")
    async def runner_heartbeat(job_id: str, req: Optional[HeartbeatRequest] = None,
                               runner: Runner = Depends(runner_auth)) -> dict:
        repo = RemoteJobRepo(pool())
        job = await own_job(job_id, runner)
        if job.status != "leased":
            return {"status": "cancelled"}
        if not await job_still_wanted(job):
            await repo.cancel(job.id)
            return {"status": "cancelled"}
        beat = await repo.heartbeat(job.id, runner.id, _now(),
                                    state.settings.RUNNER_LEASE_SECONDS)
        return {"status": "leased" if beat is not None and beat.status == "leased"
                else "cancelled"}

    @router.post("/runner/jobs/{job_id}/result")
    async def runner_result(job_id: str, request: Request,
                            runner: Runner = Depends(runner_auth)) -> dict:
        repo = RemoteJobRepo(pool())
        job = await repo.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        raw = await request.body()
        if len(raw) > R.MAX_BODY_BYTES:
            raise HTTPException(413, "result body too large")
        if job.runner_id != runner.id or job.status != "leased":
            raise HTTPException(409, "job is not leased by this runner (cancelled or expired)")
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "result is not valid JSON")
        limits = job.payload.get("limits") or {}
        try:
            result = R.validate_result(
                body, max_files=limits.get("max_files", R.MAX_FILES),
                max_file_bytes=limits.get("max_file_bytes", R.MAX_FILE_BYTES),
                max_total_bytes=limits.get("max_total_bytes", R.MAX_TOTAL_BYTES))
        except R.ResultError as exc:
            raise HTTPException(400, str(exc))
        root = services.ensure_workspace(state.settings, job.swarm_id)
        await asyncio.to_thread(R.stage_files, root, job.id, result.files)
        done = await repo.complete(job.id, runner.id, R.stored_result(result))
        if done is None:   # lost the lease while we validated
            await asyncio.to_thread(shutil.rmtree, R.staging_dir(root, job.id), True)
            raise HTTPException(409, "job is not leased by this runner (cancelled or expired)")
        return {"status": "completed"}

    return router
