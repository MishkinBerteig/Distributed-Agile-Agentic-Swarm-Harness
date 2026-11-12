"""Agent hosts (docs/DESIGN-0.4.md section 5.9): a DAASH runner installed next to existing agents
that nobody can connect to (a production machine whose firewall must not change). The owner runs
one command there; the runner dials out, reports the machine's agents and relays DAASH work to them.

* Enrollment: `POST /runner-enrollments` (admin) mints a one-time code (60 minutes; only its
  sha256 is stored) and returns the owner's single command. `POST /runner/enroll` (agent port; the
  code is the credential) turns the code into a new runner and its runner token, once.
* `GET /runner/daash-runner.pyz` (agent port, no token: it is public code) serves the runner as a
  zipapp built into the image (`python -m daash_runner.pyz`).
* Hello with `"agents": [{"kind", "ref", "name", "available", "detail"}]` upserts them as connected
  agents with protocol `runner`, already `connected` (installing the runner was the owner's
  approval). New agents get a `dca_` MCP token, returned once in the hello response
  (`agent_tokens`), so the runner can give them DAASH's tools.
* Work: a `runner` connected agent's job is a `connected` remote job whose payload names
  `runner_id`, `relay` and `local_ref`; only that runner may lease it (`lease_pinned`).
* `POST /runner/disconnect` (runner token): the owner ran `disconnect`; the runner is disabled and
  its agents go offline (their rows stay, so swarm configs keep their ids; reconnecting the same
  machine adopts them again).

Everything here reads and writes `connected_agents` with its own SQL and returns plain dicts, so
it does not depend on the shape of `app.models.ConnectedAgent`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import remote as R
from app.api_state import require_pool
from app.mcp_server import new_connected_token
from app.models import CONNECTED_HARNESS, RemoteJob
from app.repo import REMOTE_JOB_COLS, RUNNER_COLS, _to

log = logging.getLogger("daash.agent_hosts")

ENROLL_CODE_PREFIX = "dre_"
ENROLL_MINUTES = 60
ZIPAPP_NAME = "daash-runner.pyz"
ZIPAPP_ROUTE = "/runner/" + ZIPAPP_NAME
#: Default location of the zipapp inside the coordinator image (Dockerfile).
DEFAULT_ZIPAPP_PATH = "/opt/daash/" + ZIPAPP_NAME
#: Agent kind -> relay adapter of the runner (daash_runner/relay.py).
RELAYS = {"openclaw": "openclaw-agent", "hermes": "hermes-agent"}
HOST_KINDS = tuple(RELAYS)
#: Protocol of connected agents on an agent host (app.models.CONNECTED_PROTOCOLS).
RUNNER_PROTOCOL = "runner"
#: What an agent on an agent host can do by default: OpenClaw (coding tools) and Hermes
#: (terminal tool) both edit files and run commands in the job folder.
DEFAULT_CAPABILITIES = ["edit_files", "run_code"]
MAX_REPORTED_AGENTS = 50
MAX_NAME = 80
MAX_REF = 200
MAX_DETAIL = 500
GONE_ERROR = "The agent host no longer reports this agent."
DISCONNECTED_ERROR = "The agent host was disconnected by its owner."
DISABLED_ERROR = "The agent host's runner is disabled in DAASH."

RUNNER_AGENT_COLS = ("id, name, protocol, kind, status, runner_id, local_ref, capabilities, "
                     "max_jobs, enabled, card, last_ok_at, last_error, mcp_seen_at, created_at")
ENROLLMENT_COLS = "id, created_at, expires_at, used_at, runner_id"


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

def new_enroll_code() -> str:
    """`dre_` + 24 url-safe characters (144 bits). The prefix keeps it from starting with `-`
    on the owner's command line."""
    return ENROLL_CODE_PREFIX + secrets.token_urlsafe(18)


def hash_code(code: str) -> str:
    return hashlib.sha256((code or "").strip().encode("utf-8")).hexdigest()


def owner_command(public_url: str, code: str) -> str:
    """The single command the owner pastes on the agent's machine."""
    base = public_url.strip().rstrip("/")
    return (f"curl -fsSLo ~/{ZIPAPP_NAME} {base}{ZIPAPP_ROUTE} && "
            f"python3 ~/{ZIPAPP_NAME} connect {base} {code}")


def enrollment_status(row: Mapping[str, Any], now: datetime) -> str:
    """pending | used | expired."""
    if row.get("used_at") is not None:
        return "used"
    return "expired" if row["expires_at"] <= now else "pending"


def _clean_text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return text[:limit]


def normalize_reported_agents(raw: Any) -> list[dict[str, Any]]:
    """The hello's `agents` cleaned up: known kinds only (`openclaw`, `hermes`), a non-empty ref,
    a name (default: the ref), `available` (default true), a short `detail`, and the optional
    runner hints `tools` (DAASH tools are installed) and `new_token` (please mint a new MCP token).
    Malformed entries are ignored; duplicates (same kind and ref) keep the first."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        ref = _clean_text(item.get("ref"), MAX_REF)
        if kind not in HOST_KINDS or not ref or (kind, ref) in seen:
            continue
        seen.add((kind, ref))
        out.append({
            "kind": kind, "ref": ref,
            "name": _clean_text(item.get("name"), MAX_NAME) or ref,
            "available": item.get("available") is not False,
            "detail": _clean_text(item.get("detail"), MAX_DETAIL),
            "tools": item.get("tools") is True,
            "new_token": item.get("new_token") is True,
        })
        if len(out) >= MAX_REPORTED_AGENTS:
            break
    return out


def split_local_ref(local_ref: str) -> tuple[Optional[str], str]:
    """`local_ref` is the agent's local id ("main", a Hermes profile); only when two kinds on one
    host share an id is it stored as "<kind>:<id>". Returns (kind or None, id)."""
    kind, sep, rest = (local_ref or "").partition(":")
    if sep and kind in HOST_KINDS and rest:
        return kind, rest
    return None, local_ref or ""


def unique_name(base: str, host: str, taken: Iterable[str]) -> str:
    """`base` if free, else "base (host)", else "base (host) 2", 3, ... (case-insensitive)."""
    used = {t.casefold() for t in taken}
    base = (base or "Agent")[:MAX_NAME]
    if base.casefold() not in used:
        return base
    first = f"{base} ({host})" if host else f"{base} (2)"
    if first.casefold() not in used:
        return first
    n = 2
    while f"{first} {n}".casefold() in used:
        n += 1
    return f"{first} {n}"


def unique_runner_name(requested: str, taken: Iterable[str]) -> str:
    """A runner name: the requested one (or the host), then "-2", "-3", ..."""
    used = set(taken)
    base = (_clean_text(requested, 70) or "agent-host")
    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def runner_agent_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """A runner-hosted connected agent as JSON (no token material)."""
    d = dict(row)
    card = d.get("card")
    if isinstance(card, str):
        try:
            card = json.loads(card)
        except ValueError:
            card = None
    card = card if isinstance(card, dict) else {}
    kind, ref = split_local_ref(d.get("local_ref") or "")
    out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()
           if k not in ("card",)}
    out.update({"ref": ref, "relay": RELAYS.get(d.get("kind") or "", None),
                "host": card.get("host"), "detail": card.get("detail") or "",
                "available": card.get("available", True), "tools": bool(card.get("tools")),
                "reported_name": card.get("reported_name")})
    return out


def effective_status(stored: str, runner_enabled: bool, runner_last_seen: Optional[datetime],
                     now: datetime, online_seconds: int = 3 * R.ONLINE_SECONDS) -> str:
    """What DAASHboard should show for a runner-hosted agent: its stored status, but `offline`
    while its runner is disabled or has not been heard from (hello every 5 minutes, leases and
    heartbeats in between, so a few minutes of silence means the machine or runner is down)."""
    if not runner_enabled or runner_last_seen is None:
        return "offline"
    if (now - runner_last_seen).total_seconds() > online_seconds:
        return "offline"
    return stored


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class EnrollError(Exception):
    """A code that cannot be redeemed: `reason` is unknown | used | expired."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


class AgentHostRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    # -- enrollments -------------------------------------------------------------------------

    async def create_enrollment(self, code: str, now: datetime,
                                minutes: int = ENROLL_MINUTES) -> dict:
        row = await self._pool.fetchrow(
            f"INSERT INTO runner_enrollments (id, code_hash, created_at, expires_at) "
            f"VALUES ($1, $2, $3, $4) RETURNING {ENROLLMENT_COLS}",
            _uid(), hash_code(code), now, now + timedelta(minutes=minutes))
        return dict(row)

    async def list_enrollments(self, include_all: bool = False, now: Optional[datetime] = None,
                               limit: int = 50) -> list[dict]:
        now = now or _now()
        if include_all:
            rows = await self._pool.fetch(
                f"SELECT {ENROLLMENT_COLS} FROM runner_enrollments ORDER BY created_at DESC "
                f"LIMIT $1", limit)
        else:
            rows = await self._pool.fetch(
                f"SELECT {ENROLLMENT_COLS} FROM runner_enrollments WHERE used_at IS NULL "
                f"AND expires_at > $1 ORDER BY created_at DESC LIMIT $2", now, limit)
        return [dict(r) for r in rows]

    async def get_enrollment(self, enrollment_id: str) -> Optional[dict]:
        row = await self._pool.fetchrow(
            f"SELECT {ENROLLMENT_COLS} FROM runner_enrollments WHERE id = $1", enrollment_id)
        return dict(row) if row else None

    async def delete_enrollment(self, enrollment_id: str) -> Optional[str]:
        """Revoke a pending code. Returns None when deleted, else why not (missing | used)."""
        row = await self.get_enrollment(enrollment_id)
        if row is None:
            return "missing"
        if row["used_at"] is not None:
            return "used"
        await self._pool.execute("DELETE FROM runner_enrollments WHERE id = $1 AND used_at IS NULL",
                                 enrollment_id)
        return None

    async def enroll(self, code: str, now: datetime, requested_name: str, host: str,
                     version: str = "") -> tuple[dict, str]:
        """Redeem a code: (runner row, runner token). Raises EnrollError. The code works once:
        the enrollment is marked used in the same transaction that creates the runner."""
        token = R.new_runner_token()
        async with self._pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "UPDATE runner_enrollments SET used_at = $2 WHERE code_hash = $1 "
                "AND used_at IS NULL AND expires_at > $2 RETURNING id", hash_code(code), now)
            if row is None:
                old = await conn.fetchrow(
                    "SELECT used_at, expires_at FROM runner_enrollments WHERE code_hash = $1",
                    hash_code(code))
                if old is None:
                    raise EnrollError("unknown")
                raise EnrollError("used" if old["used_at"] is not None else "expired")
            names = [r["name"] for r in await conn.fetch("SELECT name FROM runners")]
            name = unique_runner_name(requested_name or host, names)
            runner = await conn.fetchrow(
                f"INSERT INTO runners (id, name, token_hash, max_jobs, host, version) "
                f"VALUES ($1, $2, $3, 1, $4, $5) RETURNING {RUNNER_COLS}",
                _uid(), name, R.hash_token(token), host[:200] or None, version[:80] or None)
            await conn.execute("UPDATE runner_enrollments SET runner_id = $2 WHERE id = $1",
                               row["id"], runner["id"])
        return dict(runner), token

    # -- runner-hosted agents ------------------------------------------------------------------

    async def agents_of_runner(self, runner_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            f"SELECT {RUNNER_AGENT_COLS} FROM connected_agents WHERE runner_id = $1 "
            f"ORDER BY created_at, name", runner_id)
        return [dict(r) for r in rows]

    async def set_runner_agents_offline(self, runner_id: str, reason: str) -> int:
        res = await self._pool.execute(
            "UPDATE connected_agents SET status = 'offline', last_error = $2 "
            "WHERE runner_id = $1 AND protocol = $3", runner_id, reason, RUNNER_PROTOCOL)
        return int(res.split()[-1])

    async def disconnect_runner(self, runner_id: str) -> int:
        """The owner disconnected the machine: disable the runner (its token stops working) and
        put its agents offline. Queued work pinned to it is left for the engine's fallback."""
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE runners SET enabled = false WHERE id = $1", runner_id)
            res = await conn.execute(
                "UPDATE connected_agents SET status = 'offline', last_error = $2 "
                "WHERE runner_id = $1 AND protocol = $3", runner_id, DISCONNECTED_ERROR,
                RUNNER_PROTOCOL)
        return int(res.split()[-1])

    async def sync_agents(self, runner_id: str, host: str, reported: list[dict],
                          now: Optional[datetime] = None) -> dict[str, Any]:
        """Upsert what the runner reported (normalize_reported_agents) as `runner` connected
        agents of this runner. Returns {"agents": [view...], "tokens": {local_ref: dca token}}.

        * A known agent (same kind and ref on this runner) is updated: status `connected` when
          available, else `offline` with the runner's detail as `last_error`.
        * An agent not yet known on this runner is first looked for on another runner of the
          same host (the owner disconnected and connected again): that row is adopted, so its id
          (and every swarm slot naming it) stays. Otherwise it is created, named after the
          agent, or "Name (host)" when the name is taken.
        * New and adopted agents, and agents the runner asks a `new_token` for, get a new
          `dca_` MCP token: only its hash is stored, the token is returned once.
        * This runner's agents that were not reported go `offline`."""
        now = now or _now()
        tokens: dict[str, str] = {}
        views: list[dict] = []
        async with self._pool.acquire() as conn, conn.transaction():
            mine = await conn.fetch(
                f"SELECT {RUNNER_AGENT_COLS} FROM connected_agents WHERE runner_id = $1 "
                f"FOR UPDATE", runner_id)
            by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
            refs_in_use: set[str] = set()
            for r in mine:
                refs_in_use.add(r["local_ref"])
                _, ref = split_local_ref(r["local_ref"])
                by_key[(r["kind"], ref)] = r
            touched: set[str] = set()
            for a in reported:
                card = {"host": host, "detail": a["detail"], "available": a["available"],
                        "reported_name": a["name"], "tools": a["tools"],
                        "relay": RELAYS[a["kind"]]}
                status = "connected" if a["available"] else "offline"
                last_error = None if a["available"] else (a["detail"] or "not available")
                row = by_key.get((a["kind"], a["ref"]))
                mint = a["new_token"]
                if row is None:
                    row = await self._adoptable(conn, runner_id, host, a["kind"], a["ref"])
                    mint = True
                    local_ref = (a["ref"] if a["ref"] not in refs_in_use
                                 else f"{a['kind']}:{a['ref']}")
                    refs_in_use.add(local_ref)
                else:
                    local_ref = row["local_ref"]
                token = new_connected_token() if mint else None
                if row is None:
                    taken = [r["name"] for r in await conn.fetch("SELECT name FROM connected_agents")]
                    row = await conn.fetchrow(
                        f"INSERT INTO connected_agents (id, name, protocol, url, kind, status, "
                        f"runner_id, local_ref, mcp_token_hash, capabilities, card, last_ok_at, "
                        f"last_error) VALUES ($1, $2, $3, NULL, $4, $5, $6, $7, $8, $9, $10, $11, "
                        f"$12) RETURNING {RUNNER_AGENT_COLS}",
                        _uid(), unique_name(a["name"], host, taken), RUNNER_PROTOCOL, a["kind"],
                        status, runner_id, local_ref, R.hash_token(token),
                        list(DEFAULT_CAPABILITIES), card, now if a["available"] else None,
                        last_error)
                else:
                    name = row["name"]
                    old_card = _jsonb(row["card"]) or {}
                    if old_card.get("reported_name") not in (None, a["name"]):
                        # the owner renamed the agent: follow, unless that name is taken
                        taken = [r["name"] for r in await conn.fetch(
                            "SELECT name FROM connected_agents WHERE id <> $1", row["id"])]
                        name = unique_name(a["name"], host, taken)
                    row = await conn.fetchrow(
                        f"UPDATE connected_agents SET runner_id = $2, local_ref = $3, status = $4, "
                        f"card = $5, name = $6, last_error = $7, "
                        f"last_ok_at = CASE WHEN $8 THEN $9::timestamptz ELSE last_ok_at END, "
                        f"mcp_token_hash = COALESCE($10, mcp_token_hash) "
                        f"WHERE id = $1 RETURNING {RUNNER_AGENT_COLS}",
                        row["id"], runner_id, local_ref, status, card, name, last_error,
                        a["available"], now, R.hash_token(token) if token else None)
                if token:
                    tokens[local_ref] = token
                touched.add(row["id"])
                views.append(runner_agent_view(row))
            gone = [r["id"] for r in mine if r["id"] not in touched and r["status"] != "offline"]
            if gone:
                await conn.execute(
                    "UPDATE connected_agents SET status = 'offline', last_error = $2 "
                    "WHERE id = ANY($1::text[])", gone, GONE_ERROR)
        return {"agents": views, "tokens": tokens}

    @staticmethod
    async def _adoptable(conn, runner_id: str, host: str, kind: str, ref: str):
        """A runner agent of the same host, kind and ref that belongs to another runner (the
        machine was connected again), locked for update; None if there is none."""
        return await conn.fetchrow(
            f"SELECT {RUNNER_AGENT_COLS} FROM connected_agents WHERE protocol = $1 AND kind = $2 "
            f"AND (local_ref = $3 OR local_ref = $4) AND card->>'host' = $5 "
            f"AND runner_id IS DISTINCT FROM $6 ORDER BY (status = 'offline') DESC, created_at "
            f"LIMIT 1 FOR UPDATE",
            RUNNER_PROTOCOL, kind, ref, f"{kind}:{ref}", host, runner_id)

    # -- work pinned to one runner ----------------------------------------------------------------

    async def lease_pinned(self, runner_id: str, now: datetime,
                           lease_seconds: int = R.LEASE_SECONDS) -> Optional[RemoteJob]:
        """Lease the oldest queued `connected` job of an ACTIVE swarm whose payload names this
        runner (an agent host relays it to one of its agents). No MCP job token is minted: the
        agent uses its own `dca_` token (docs/DESIGN-0.4.md 5.9)."""
        async with self._pool.acquire() as conn, conn.transaction():
            pick = await conn.fetchrow(
                "SELECT j.id FROM remote_jobs j JOIN swarms s ON s.id = j.swarm_id "
                "WHERE j.status = 'queued' AND j.harness = $1 AND j.payload->>'runner_id' = $2 "
                "AND s.lifecycle_state = 'ACTIVE' ORDER BY j.created_at, j.id "
                "FOR UPDATE OF j SKIP LOCKED LIMIT 1", CONNECTED_HARNESS, runner_id)
            if pick is None:
                return None
            row = await conn.fetchrow(
                f"UPDATE remote_jobs SET status = 'leased', runner_id = $2, "
                f"leased_at = $3::timestamptz, heartbeat_at = $3::timestamptz, "
                f"lease_expires_at = $3::timestamptz + make_interval(secs => $4::double precision) "
                f"WHERE id = $1 RETURNING {REMOTE_JOB_COLS}",
                pick["id"], runner_id, now, float(lease_seconds))
        return _to(RemoteJob, row)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class EnrollRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    host: str = Field("", max_length=200)
    name: str = Field("", max_length=80)
    version: str = Field("", max_length=80)
    platform: str = Field("", max_length=200)


ENROLL_ERRORS = {
    "unknown": (404, "This connect code is not valid. Create a new one in DAASHboard (Agents & "
                     "models, Connect agents on another machine)."),
    "used": (410, "This connect code was already used. Each code works once: create a new one in "
                  "DAASHboard."),
    "expired": (410, f"This connect code expired (codes are valid for {ENROLL_MINUTES} minutes). "
                     "Create a new one in DAASHboard."),
}


def zipapp_path(settings: Any) -> Path:
    return Path(getattr(settings, "RUNNER_ZIPAPP_PATH", "") or DEFAULT_ZIPAPP_PATH)


def _enrollment_json(row: Mapping[str, Any], now: datetime) -> dict:
    out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
    out["status"] = enrollment_status(row, now)
    return out


def build_admin_router(state: Any) -> APIRouter:
    """Admin routes (coordinator port, admin token): create, list, inspect and revoke one-time
    connect codes for agent hosts."""
    router = APIRouter()

    def repo() -> AgentHostRepo:
        return AgentHostRepo(require_pool(state))

    @router.post("/runner-enrollments", status_code=201)
    async def create_enrollment() -> dict:
        public = (state.settings.AGENT_PUBLIC_URL or "").strip().rstrip("/")
        if not public:
            from app.api_state import agent_endpoint_info
            raise HTTPException(409, "DAASH_AGENT_PUBLIC_URL is not set, so DAASH cannot tell the "
                                     "agent's machine where to find it. "
                                     + agent_endpoint_info(None)["how_to_set"])
        code = new_enroll_code()
        now = _now()
        row = await repo().create_enrollment(code, now)
        return {**_enrollment_json(row, now), "code": code, "command": owner_command(public, code),
                "public_url": public, "minutes": ENROLL_MINUTES}

    @router.get("/runner-enrollments")
    async def list_enrollments(all: bool = False) -> dict:  # noqa: A002 - query parameter name
        now = _now()
        rows = await repo().list_enrollments(include_all=all, now=now)
        return {"enrollments": [_enrollment_json(r, now) for r in rows]}

    @router.get("/runner-enrollments/{enrollment_id}")
    async def get_enrollment(enrollment_id: str) -> dict:
        """One code: its status and, once used, the runner it created and the agents that
        runner reports (DAASHboard polls this after showing the command)."""
        r = repo()
        row = await r.get_enrollment(enrollment_id)
        if row is None:
            raise HTTPException(404, "enrollment not found")
        now = _now()
        out = _enrollment_json(row, now)
        out["runner"], out["agents"] = None, []
        if row["runner_id"]:
            runner = await require_pool(state).fetchrow(
                "SELECT id, name, enabled, host, version, last_seen_at FROM runners WHERE id = $1",
                row["runner_id"])
            if runner is not None:
                last = runner["last_seen_at"]
                out["runner"] = {
                    "id": runner["id"], "name": runner["name"], "enabled": runner["enabled"],
                    "host": runner["host"], "version": runner["version"],
                    "last_seen_at": last.isoformat() if last else None,
                    "online": bool(runner["enabled"] and last is not None
                                   and (now - last).total_seconds() <= R.ONLINE_SECONDS)}
                out["agents"] = [runner_agent_view(a)
                                 for a in await r.agents_of_runner(row["runner_id"])]
        return out

    @router.delete("/runner-enrollments/{enrollment_id}", status_code=204)
    async def delete_enrollment(enrollment_id: str) -> Response:
        why = await repo().delete_enrollment(enrollment_id)
        if why == "missing":
            raise HTTPException(404, "enrollment not found")
        if why == "used":
            raise HTTPException(409, "this code was already used; disable or delete its runner "
                                     "instead")
        return Response(status_code=204)

    return router


def build_agent_router(state: Any) -> APIRouter:
    """Routes for the machine that becomes an agent host (both ports; under /runner/, so the
    admin token never applies): the zipapp download, enrollment and disconnect."""
    router = APIRouter()

    @router.get(ZIPAPP_ROUTE)
    async def download_zipapp() -> Response:
        path = zipapp_path(state.settings)
        if not path.is_file():
            raise HTTPException(404, f"The runner zipapp is missing from this DAASH image ({path}). "
                                     "Rebuild the image: docker compose up --build -d")
        return FileResponse(path, media_type="application/zip", filename=ZIPAPP_NAME)

    @router.post("/runner/enroll", status_code=201)
    async def enroll(req: EnrollRequest) -> dict:
        try:
            runner, token = await AgentHostRepo(require_pool(state)).enroll(
                req.code, _now(), req.name.strip(), req.host.strip(), req.version.strip())
        except EnrollError as exc:
            status, text = ENROLL_ERRORS[exc.reason]
            raise HTTPException(status, text)
        log.info("agent host enrolled: runner '%s' on %s", runner["name"], runner["host"])
        return {"runner_id": runner["id"], "name": runner["name"], "token": token}

    @router.post("/runner/disconnect")
    async def disconnect(request: Request) -> dict:
        pool = require_pool(state)
        token = R.bearer_token(request.headers.get("authorization"))
        row = await pool.fetchrow("SELECT id, enabled FROM runners WHERE token_hash = $1",
                                  R.hash_token(token)) if token else None
        if row is None or not row["enabled"]:
            raise HTTPException(401, "Invalid runner token", headers={"WWW-Authenticate": "Bearer"})
        n = await AgentHostRepo(pool).disconnect_runner(row["id"])
        return {"status": "disconnected", "agents_offline": n}

    return router


__all__ = [
    "AgentHostRepo", "EnrollError", "build_admin_router", "build_agent_router", "effective_status",
    "new_enroll_code", "normalize_reported_agents", "owner_command", "runner_agent_view",
    "split_local_ref", "unique_name", "unique_runner_name", "RELAYS", "RUNNER_PROTOCOL",
]
