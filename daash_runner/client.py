"""HTTP client for the coordinator's /runner/* endpoints (urllib only)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class CoordinatorError(Exception):
    """Network failure or 5xx: retry with backoff."""


class AuthError(Exception):
    """401: the token is wrong, unknown or disabled."""


class HttpStatus(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class Client:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _open(self, method: str, path: str, body: bytes | None, ctype: str | None, timeout: float):
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - operator-given URL
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                raise AuthError(text) from exc
            if exc.code >= 500:
                raise CoordinatorError(f"HTTP {exc.code}: {text[:200]}") from exc
            raise HttpStatus(exc.code, text) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise CoordinatorError(str(exc)) from exc

    def call(self, method: str, path: str, payload: Any = None, timeout: float | None = None) -> tuple[int, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        with self._open(method, path, body, "application/json" if body else None, timeout or self.timeout) as r:
            raw = r.read()
            status = r.status
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError as exc:
            raise CoordinatorError(f"bad JSON from coordinator: {exc}") from exc

    # -- protocol (DESIGN-0.3 section 5) ----------------------------------

    def hello(self, host: str, version: str, max_jobs: int, harnesses: list[dict],
              agents: list[dict] | None = None) -> dict:
        """`agents` (an agent host's local agents, docs/DESIGN-0.4.md 5.9) is sent only when
        given; the answer then carries `agents` and `agent_tokens`."""
        body = {"host": host, "version": version, "max_jobs": max_jobs, "harnesses": harnesses}
        if agents is not None:
            body["agents"] = agents
        return self.call("POST", "/runner/hello", body)[1] or {}

    def enroll(self, code: str, host: str, name: str, version: str, platform: str) -> dict:
        """Exchange an agent host's one-time connect code for a runner token (no token yet)."""
        return self.call("POST", "/runner/enroll", {
            "code": code, "host": host, "name": name, "version": version,
            "platform": platform})[1] or {}

    def health(self) -> dict:
        return self.call("GET", "/health", None, timeout=10)[1] or {}

    def disconnect(self) -> dict:
        """Tell the coordinator this agent host is going away (its runner is disabled)."""
        return self.call("POST", "/runner/disconnect", {})[1] or {}

    def lease(self, wait_seconds: int, free_slots: int) -> dict | None:
        status, data = self.call("POST", "/runner/lease",
                                 {"wait_seconds": wait_seconds, "free_slots": free_slots},
                                 timeout=wait_seconds + 20)
        return data.get("job") if status == 200 and data else None

    def download_workspace(self, job_id: str, dest_path: str, url: str | None = None) -> None:
        with self._open("GET", url or f"/runner/jobs/{job_id}/workspace", None, None, 300) as r, \
                open(dest_path, "wb") as out:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)

    def heartbeat(self, job_id: str, note: str | None = None) -> str:
        """Returns 'leased' | 'cancelled'. A 404/409 means the job is no longer ours -> 'cancelled'."""
        try:
            _, data = self.call("POST", f"/runner/jobs/{job_id}/heartbeat", {"note": note} if note else {})
        except HttpStatus as exc:
            if exc.status in (404, 409):
                return "cancelled"
            raise
        return (data or {}).get("status", "leased")

    def result(self, job_id: str, payload: dict) -> str:
        """Returns 'completed', or 'dropped' on 409 (job cancelled/expired). Raises HttpStatus on 400."""
        try:
            _, data = self.call("POST", f"/runner/jobs/{job_id}/result", payload, timeout=120)
        except HttpStatus as exc:
            if exc.status in (404, 409):
                return "dropped"
            raise
        return (data or {}).get("status", "completed")
