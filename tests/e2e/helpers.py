"""Clients and polling helpers for the e2e tier (docs/DESIGN-0.2.md A.3).

Everything waits on a condition with a timeout (`poll`); nothing sleeps and hopes. `hold` is the
one deliberate time-based check: it asserts a condition STAYS true for a number of seconds
(used to prove that nothing is being spent while the swarm is paused).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Optional

import httpx

COORD_URL = os.environ.get("E2E_COORDINATOR_URL", "http://coordinator:8000")
FAKE_URL = os.environ.get("E2E_FAKE_LLM_URL", "http://fake-llm:9100")
COMPOSE_PROJECT = os.environ.get("E2E_COMPOSE_PROJECT", "daash-e2e")
DOCKER_SOCK = os.environ.get("E2E_DOCKER_SOCK", "/var/run/docker.sock")

VISION = "Write a short guide to compost."


def poll(fn: Callable[[], Any], *, timeout: float, interval: float = 0.4, desc: str = "condition"):
    """Call `fn` until it returns something truthy; return it. AssertionError on timeout."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while True:
        try:
            last = fn()
        except (httpx.HTTPError, AssertionError, KeyError) as exc:   # transient while starting
            last = exc
        else:
            if last:
                return last
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {desc}; "
                                 f"last value: {last!r}")
        time.sleep(interval)


def hold(fn: Callable[[], bool], *, seconds: float, interval: float = 0.5, desc: str = "condition"):
    """Assert `fn()` stays true for `seconds` (checked every `interval`)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        assert fn(), f"{desc} stopped holding"
        time.sleep(interval)


class Api:
    """The coordinator's REST API; non-2xx raises with the body for readable failures."""

    def __init__(self, base: str = COORD_URL) -> None:
        self.http = httpx.Client(base_url=base, timeout=30)

    def request(self, method: str, path: str, expect: tuple = (200, 201, 204), **kw) -> Any:
        r = self.http.request(method, path, **kw)
        assert r.status_code in expect, f"{method} {path} -> {r.status_code}: {r.text[:500]}"
        return r.json() if r.content else None

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Optional[dict] = None, **kw):
        return self.request("POST", path, json=body if body is not None else {}, **kw)

    def patch(self, path: str, body: dict, **kw):
        return self.request("PATCH", path, json=body, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)

    # -- swarm conveniences -------------------------------------------------------------

    def create_swarm(self, vision: str = VISION, name: str = "e2e swarm", teams: tuple = (),
                     config: Optional[dict] = None, dod: Optional[str] = None) -> dict:
        body: dict = {"name": name, "vision_statement": vision, "teams": list(teams)}
        if config:
            body["config"] = config
        if dod:
            body["definition_of_done"] = dod
        return self.post("/swarms", body)

    def transition(self, swarm_id: str, action: str) -> dict:
        return self.post(f"/swarms/{swarm_id}/transitions/{action}")

    def swarm(self, swarm_id: str) -> dict:
        return self.get(f"/swarms/{swarm_id}")

    def state(self, swarm_id: str) -> str:
        return self.swarm(swarm_id)["lifecycle_state"]

    def tasks(self, swarm_id: str, **params) -> list[dict]:
        return self.get("/tasks", params={"swarm_id": swarm_id, **params})["tasks"]

    def events(self, swarm_id: str, limit: int = 1000, **params) -> list[dict]:
        return self.get(f"/swarms/{swarm_id}/events", params={"limit": limit, **params})["events"]

    def wait_state(self, swarm_id: str, state: str, timeout: float = 120) -> dict:
        poll(lambda: self.state(swarm_id) == state, timeout=timeout,
             desc=f"swarm {swarm_id[:8]} state {state}")
        return self.swarm(swarm_id)

    def wait_verification(self, swarm_id: str, timeout: float = 150) -> dict:
        try:
            return self.wait_state(swarm_id, "VERIFICATION", timeout)
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n{self.dump(swarm_id)}") from None

    def dump(self, swarm_id: str) -> str:
        try:
            tasks = self.tasks(swarm_id)
            evs = self.events(swarm_id, limit=15)
            sw = self.swarm(swarm_id)
            lines = [f"swarm {sw['lifecycle_state']} paused_reason={sw['paused_reason']!r}"]
            lines += [f"  task {t['name']!r} {t['status']} attempts={t['attempts']}" for t in tasks]
            lines += [f"  event {e['kind']}: {e['detail'][:140]}" for e in evs]
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the real failure
            return f"(could not dump: {exc!r})"

    def remove_all_swarms(self) -> None:
        """Bring the coordinator back to 'no swarms' whatever state the last test left."""
        for _ in range(4):
            active = self.get("/swarms/active")
            if not active:
                break
            sid, state = active["id"], active["lifecycle_state"]
            if state == "USER_FEEDBACK":
                self.transition(sid, "resume")
            elif state in ("ACTIVE", "PAUSED"):
                self.transition(sid, "verify")
            elif state == "LEARNING":
                self.transition(sid, "archive")
            elif state in ("CREATED", "VERIFICATION"):
                self.delete(f"/swarms/{sid}")
        for s in self.get("/swarms")["swarms"]:
            if s["lifecycle_state"] == "ARCHIVED":
                self.delete(f"/swarms/{s['id']}")


class Fake:
    """The fake LLM server's control API."""

    def __init__(self, base: str = FAKE_URL) -> None:
        self.http = httpx.Client(base_url=base, timeout=30)

    def configure(self, **settings) -> dict:
        r = self.http.post("/control/scenario", json=settings)
        assert r.status_code == 200, r.text
        return r.json()

    def reset(self) -> None:
        assert self.http.post("/control/reset").status_code == 200

    def calls(self, since: int = 0) -> list[dict]:
        return self.http.get("/control/calls", params={"since": since}).json()["calls"]

    def count(self) -> int:
        return self.http.get("/control/calls", params={"since": 10**9}).json()["count"]

    def jobs(self, ok_only: bool = True) -> list[str]:
        return [c["job"] for c in self.calls() if not ok_only or c["status"] == 200]

    def wait_until_stable(self, quiet_seconds: float = 2.5, timeout: float = 30) -> int:
        """Block until no call has arrived for `quiet_seconds`; return the call count."""
        state = {"n": self.count(), "since": time.monotonic()}

        def quiet() -> bool:
            n = self.count()
            if n != state["n"]:
                state["n"], state["since"] = n, time.monotonic()
            return time.monotonic() - state["since"] >= quiet_seconds

        poll(quiet, timeout=timeout, interval=0.3, desc="the fake LLM to go quiet")
        return state["n"]


class Docker:
    """Just enough of the Docker Engine API, over the unix socket, to restart a service."""

    def __init__(self, sock: str = DOCKER_SOCK, project: str = COMPOSE_PROJECT) -> None:
        self.http = httpx.Client(transport=httpx.HTTPTransport(uds=sock),
                                 base_url="http://docker", timeout=60)
        self.project = project

    def container(self, service: str) -> dict:
        filters = json.dumps({"label": [f"com.docker.compose.project={self.project}",
                                        f"com.docker.compose.service={service}"]})
        r = self.http.get("/containers/json", params={"all": "true", "filters": filters})
        assert r.status_code == 200, r.text
        found = r.json()
        assert len(found) == 1, f"expected one {service} container in {self.project}: {found}"
        return found[0]

    def started_at(self, service: str) -> str:
        cid = self.container(service)["Id"]
        r = self.http.get(f"/containers/{cid}/json")
        assert r.status_code == 200, r.text
        return r.json()["State"]["StartedAt"]

    def running(self, service: str) -> bool:
        return self.container(service)["State"] == "running"

    def restart(self, service: str, grace_seconds: int = 2) -> None:
        cid = self.container(service)["Id"]
        r = self.http.post(f"/containers/{cid}/restart", params={"t": str(grace_seconds)})
        assert r.status_code == 204, f"restart {service}: {r.status_code} {r.text}"

    def _act(self, service: str, verb: str, ok: tuple, **params) -> None:
        cid = self.container(service)["Id"]
        r = self.http.post(f"/containers/{cid}/{verb}", params=params)
        assert r.status_code in ok, f"{verb} {service}: {r.status_code} {r.text}"

    def start(self, service: str) -> None:
        self._act(service, "start", (204, 304))

    def stop(self, service: str, grace_seconds: int = 2) -> None:
        self._act(service, "stop", (204, 304), t=str(grace_seconds))

    def kill(self, service: str) -> None:
        self._act(service, "kill", (204,))
