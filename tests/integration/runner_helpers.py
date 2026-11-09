"""An in-process fake runner for the 0.3 tests (docs/DESIGN-0.3.md section 9).

`FakeRunner` talks to the real coordinator API over the httpx ASGI client, exactly like
`daash_runner` would over HTTP: hello -> lease -> download the workspace -> write the task's
artifact -> post the result. `behaviors` scripts what each leased job does.
"""
from __future__ import annotations

import asyncio
import base64
import io
import tarfile
from typing import Any, Optional

import httpx

from app.models import AgentRole
from app.repo import RemoteJobRepo

BODY = ("Overview\n" + "This section explains the deliverable clearly and completely for the "
        "reader. " * 6)


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class FakeRunner:
    """behaviors: per leased job, popped in order; the last one repeats.
    "ok" -> submitted; "failed"; "error"; "hang" (heartbeats until told to stop, never posts);
    "vanish" (leases, then goes silent)."""

    def __init__(self, api: httpx.AsyncClient, token: str, name: str = "fake-runner",
                 harnesses: Optional[list[dict]] = None, behaviors: Optional[list[str]] = None,
                 max_jobs: int = 4) -> None:
        self.api, self.token, self.name = api, token, name
        self.harnesses = harnesses if harnesses is not None else [{"harness": "fake",
                                                                    "models": []}]
        self.behaviors = list(behaviors or ["ok"])
        self.max_jobs = max_jobs
        self.jobs: list[dict] = []              # every job leased
        self.workspaces: dict[str, list[str]] = {}   # job id -> file names in the tarball
        self.cancelled: list[str] = []          # job ids a heartbeat said "cancelled" for
        self.posted: list[tuple[str, int]] = []  # (job id, HTTP status of the result)
        self.stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def hello(self) -> httpx.Response:
        return await self.api.post("/runner/hello", headers=self.headers, json={
            "host": self.name, "version": "test", "max_jobs": self.max_jobs,
            "harnesses": self.harnesses})

    async def lease(self, wait: float = 0) -> Optional[dict]:
        r = await self.api.post("/runner/lease", headers=self.headers,
                                json={"wait_seconds": wait, "free_slots": 1})
        if r.status_code == 204:
            return None
        assert r.status_code == 200, r.text
        job = r.json()["job"]
        self.jobs.append(job)
        return job

    async def workspace(self, job: dict) -> list[str]:
        r = await self.api.get(job["workspace_url"], headers=self.headers)
        assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
            names = tar.getnames()
        self.workspaces[job["id"]] = names
        return names

    async def heartbeat(self, job: dict) -> str:
        r = await self.api.post(f"/runner/jobs/{job['id']}/heartbeat", headers=self.headers,
                                json={})
        assert r.status_code == 200, r.text
        status = r.json()["status"]
        if status == "cancelled" and job["id"] not in self.cancelled:
            self.cancelled.append(job["id"])
        return status

    async def post_result(self, job: dict, outcome: str = "submitted", **extra: Any) -> int:
        artifact = job["task"]["artifact"]
        body = {"outcome": outcome, "summary": f"{self.name} did {job['task']['name']}",
                "files": [{"path": artifact, "content_b64": b64(BODY)}] if artifact else [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20}, "duration_ms": 5,
                "exit_code": 0 if outcome == "submitted" else 1, "transcript_tail": "tail"}
        if outcome != "submitted":
            body["error"] = f"{outcome} (scripted)"
        body.update(extra)
        r = await self.api.post(f"/runner/jobs/{job['id']}/result", headers=self.headers,
                                json=body)
        self.posted.append((job["id"], r.status_code))
        return r.status_code

    async def handle(self, job: dict) -> None:
        mode = self.behaviors.pop(0) if len(self.behaviors) > 1 else self.behaviors[0]
        await self.workspace(job)
        if mode == "vanish":
            return
        if mode == "hang":
            while not self.stop.is_set():
                if await self.heartbeat(job) == "cancelled":
                    return
                await asyncio.sleep(0.02)
            return
        await self.heartbeat(job)
        await self.post_result(job, {"ok": "submitted"}.get(mode, mode))

    async def run(self) -> None:
        await self.hello()
        while not self.stop.is_set():
            job = await self.lease(0)
            if job is None:
                await asyncio.sleep(0.02)
                continue
            self._tasks.append(asyncio.create_task(self.handle(job)))

    def start(self) -> "FakeRunner":
        self._tasks.append(asyncio.create_task(self.run()))
        return self

    async def close(self) -> None:
        self.stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


async def new_runner(api: httpx.AsyncClient, name: str, admin: Optional[dict] = None
                     ) -> tuple[dict, str]:
    """POST /runners -> (runner json, token)."""
    r = await api.post("/runners", json={"name": name}, headers=admin or {})
    assert r.status_code == 201, r.text
    return r.json()["runner"], r.json()["token"]


async def make_remote_job(h, pool, harness: str = "fake", model: str = "fast",
                          task_name: str = "Remote task", team=None, artifact: str = "r.md",
                          required: tuple = ()):
    """A swarm with one In-Progress task and a queued remote job for it, built without the
    engine. Returns (job, task). Reuses the swarm of `h` if one exists."""
    try:
        await h.live()
    except AssertionError:
        from tests.integration.helpers import swarm_config
        await h.create_swarm(config=swarm_config(1), dod="- The guide exists")
        await h.transition("start")
    team = team or (await h.team_list() or [await h.seed_team("Team A")])[0]
    agent = next(a for a in await h.agent_list(team) if a.role == AgentRole.MISSION_DELIVERY.value)
    task = await h.add_task(team, task_name, artifact=artifact, status="In-Progress",
                            assigned_agent_id=agent.id, complexity="moderate")
    payload = {"system_prompt": "sys", "prompt": "do it",
               "limits": {"timeout_seconds": 1800, "max_files": 200, "max_file_bytes": 2_000_000,
                          "max_total_bytes": 20_000_000},
               "required_capabilities": list(required),
               "task": {"name": task.name, "artifact": task.artifact}}
    job = await RemoteJobRepo(pool).ensure_active(
        task.swarm_id, team.id, task.id, agent.id, 0, harness, model, payload)
    return job, task


async def job_row(pool, job_id: str):
    return await RemoteJobRepo(pool).get(job_id)


__all__ = ["FakeRunner", "new_runner", "make_remote_job", "job_row", "BODY", "b64"]
