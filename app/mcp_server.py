"""The DAASH MCP server (docs/DESIGN-0.4.md section 2): DAASH's agent tools for any MCP client.

Transport: MCP streamable HTTP at `POST /mcp`, stateless (no `Mcp-Session-Id`) and answered
with plain JSON (never an SSE stream); `GET /mcp` and `DELETE /mcp` -> 405. A small JSON-RPC 2.0
handler covers `initialize` (protocol version negotiation), `notifications/initialized` and
other notifications, `ping`, `tools/list` and `tools/call` (plus empty `resources/list`,
`resources/templates/list` and `prompts/list` for clients that ask anyway). Tool problems come
back as `isError: true` results with the message, never as HTTP errors.

Why not the `mcp` SDK: DAASH's tool set and identity are per request (a bearer token names one
job or one connected agent, and every call re-checks that the job is still live), the server
must answer 401 before any protocol work, and the SDK's streamable-HTTP manager needs its own
task group started in the app lifespan. The protocol subset above is small, so a hand-rolled
handler is simpler to mount on two apps and to test, and adds no dependency.

Identity (section 2.3), `Authorization: Bearer ...`; missing or unknown -> HTTP 401:
* job token `djt_...`: minted when a runner leases a remote job (app.runner_api); only its
  sha256 is stored (`remote_jobs.mcp_token_hash`). Its tools work only while the job is leased.
* connected-agent token `dca_...`: one per connected agent (`connected_agents.mcp_token_hash`).
  It maps to the agent's active connected jobs (remote_jobs rows with harness "connected" and
  `payload.connected_agent_id`); every tool takes an optional `job` argument to pick one.
Every call re-checks the job, the swarm (ACTIVE) and the task (In-Progress), records a `tool`
event, counts `payload.mcp_calls` and, for connected agents, sets `mcp_seen_at`.
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app import agent_tools
from app.agent_tools import AgentContext, Repos, build_agent_tools, tool_specs
from app.models import CONNECTED_HARNESS, ConnectedAgent, RemoteJob, SwarmStatus, TaskStatus
from app.remote import bearer_token, hash_token
from app.tools import ToolRegistry

log = logging.getLogger("daash.mcp")

MCP_PATH = "/mcp"
#: Protocol of agents reached through a runner or an agent host (docs/DESIGN-0.4.md 5.9).
RUNNER_PROTOCOL = "runner"
JOB_TOKEN_PREFIX = "djt_"
CONNECTED_TOKEN_PREFIX = "dca_"
#: Newest first; a client asking for another version is offered the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "daash", "title": "DAASH", "version": "0.4.0"}
MAX_BODY_BYTES = 4 * 1024 * 1024
JOB_ENDED = ("This DAASH job has ended (the swarm was paused, or the task moved on); stop "
             "working on it.")
#: Job statuses in which a token's tools work: a runner job only while leased; a connected job
#: from creation (it is never leased by a runner) until it completes or is cancelled.
ACTIVE_STATUSES = {"job": ("leased",), "connected": ("queued", "leased")}

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Tokens and pure rules
# ---------------------------------------------------------------------------

def new_job_token() -> str:
    """`djt_` + 43 url-safe characters (256 bits); minted at lease, returned once."""
    return JOB_TOKEN_PREFIX + secrets.token_urlsafe(32)


def new_connected_token() -> str:
    """`dca_` + 43 url-safe characters; set once in a connected agent's own MCP config."""
    return CONNECTED_TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_kind(token: str) -> Optional[str]:
    """"job", "connected" or None (not a DAASH MCP token)."""
    if token.startswith(JOB_TOKEN_PREFIX) and len(token) > len(JOB_TOKEN_PREFIX):
        return "job"
    if token.startswith(CONNECTED_TOKEN_PREFIX) and len(token) > len(CONNECTED_TOKEN_PREFIX):
        return "connected"
    return None


@dataclass
class Identity:
    """Who holds a valid token: one runner job, or one connected agent."""
    kind: str                                   # "job" | "connected"
    job_id: Optional[str] = None                # job tokens
    connected: Optional[ConnectedAgent] = None  # connected-agent tokens

    @property
    def file_tools(self) -> bool:
        """Directly connected agents edit the live swarm workspace through MCP; agents on a
        runner or an agent host (protocol `runner`, section 5.9) work on a local copy."""
        return (self.kind == "connected" and self.connected is not None
                and self.connected.protocol != RUNNER_PROTOCOL)


def negotiate_version(requested: Any) -> str:
    """The client's protocol version when supported, else the newest one DAASH speaks."""
    return requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]


def job_ended(kind: str, job_status: str, swarm_state: Optional[str],
              task_status: Optional[str]) -> bool:
    """True once a token's job may no longer use DAASH tools: the job finished, was cancelled,
    expired or abandoned (or, for a runner job, was requeued), the swarm left ACTIVE, or the task
    is no longer In-Progress."""
    return (job_status not in ACTIVE_STATUSES.get(kind, ())
            or swarm_state != SwarmStatus.ACTIVE.value
            or task_status != TaskStatus.IN_PROGRESS.value)


def pick_connected_job(active_ids: list[str], requested: Optional[str]
                       ) -> tuple[Optional[str], Optional[str]]:
    """(job id, None) or (None, the message for the agent) for a connected agent's call."""
    listing = ", ".join(active_ids) or "none"
    if requested:
        if requested in active_ids:
            return requested, None
        return None, (f"DAASH job {requested} is not one of your active DAASH jobs "
                      f"(active: {listing}).")
    if len(active_ids) == 1:
        return active_ids[0], None
    if not active_ids:
        return None, ("You have no active DAASH job (the swarm was paused, or the task moved "
                      "on); stop working on it. Active jobs: none.")
    return None, (f"You have {len(active_ids)} active DAASH jobs ({listing}); pass job=<id> "
                  "with the job id from your work prompt.")


def mcp_tools(workspace: bool, job_arg: bool) -> list[dict]:
    """tools/list entries. Connected agents get the file tools and an optional `job` argument
    on every tool."""
    out = []
    for spec in tool_specs(workspace=workspace):
        fn = spec["function"]
        schema = json.loads(json.dumps(fn["parameters"]))   # a private copy
        if job_arg:
            schema.setdefault("properties", {})["job"] = {
                "type": "string",
                "description": "Your DAASH job id (from your work prompt). Needed only when you "
                               "have more than one DAASH job."}
        out.append({"name": fn["name"], "description": fn["description"], "inputSchema": schema})
    return out


def instructions(identity: Identity) -> str:
    s = ("DAASH tools for your current DAASH job: remember/recall (your persistent memory), "
         "team_roster, send_message/read_messages (talk to your team), current_task, "
         "create_subtask, propose_team, list_proposals and vote_on_proposal.")
    if identity.file_tools:
        s += (" list_files, read_file, write_file, append_file and delete_file edit the shared "
              "swarm workspace.")
    if identity.kind == "connected":
        s += " Pass job=<id> when you have more than one DAASH job."
    return s


# ---------------------------------------------------------------------------
# The backend: identities and tool calls against DAASH's database
# ---------------------------------------------------------------------------

class DaashBackend:
    """Resolves tokens and runs tool calls. `repos` are app.agent_tools.Repos (real or fake)."""

    def __init__(self, repos: Repos, *, embedder: Any = None,
                 signal: agent_tools.Signal = agent_tools.no_signal,
                 workspace_root: Optional[Callable[[str], Path]] = None) -> None:
        self.repos = repos
        self.embedder = embedder
        self.signal = signal
        self.workspace_root = workspace_root

    async def identify(self, token: str) -> Optional[Identity]:
        kind = token_kind(token or "")
        if kind == "job":
            job = await self.repos.remote_jobs.by_mcp_token_hash(hash_token(token))
            return Identity("job", job_id=job.id) if job is not None else None
        if kind == "connected":
            agent = await self.repos.connected.by_mcp_token_hash(hash_token(token))
            if agent is not None and agent.enabled:
                return Identity("connected", connected=agent)
        return None

    def list_tools(self, identity: Identity) -> list[dict]:
        return mcp_tools(workspace=identity.file_tools, job_arg=identity.kind == "connected")

    async def _job(self, identity: Identity, requested: Optional[str]
                   ) -> tuple[Optional[RemoteJob], Optional[str]]:
        jobs = self.repos.remote_jobs
        if identity.kind == "job":
            job = await jobs.get(identity.job_id)
            if job is None:
                return None, JOB_ENDED
            if requested and requested != job.id:
                return None, f"This token belongs to DAASH job {job.id}, not {requested}."
            return job, None
        agent = identity.connected
        active = await jobs.active_connected(agent.id)
        jid, problem = pick_connected_job([j.id for j in active], requested)
        if problem:
            if requested:   # one of this agent's jobs that has ended?
                old = await jobs.get(requested)
                if (old is not None and old.harness == CONNECTED_HARNESS
                        and (old.payload or {}).get("connected_agent_id") == agent.id):
                    return None, JOB_ENDED
            return None, problem
        return next(j for j in active if j.id == jid), None

    async def _context(self, identity: Identity, job: RemoteJob
                       ) -> tuple[Optional[AgentContext], Optional[str]]:
        r = self.repos
        swarm = await r.swarms.get(job.swarm_id)
        task = await r.tasks.get(job.task_id)
        if job_ended(identity.kind, job.status, swarm.lifecycle_state if swarm else None,
                     task.status if task else None):
            return None, JOB_ENDED
        team = await r.teams.get(job.team_id)
        agent = await r.agents.get(job.agent_id)
        if team is None or agent is None:
            return None, JOB_ENDED
        files = identity.file_tools

        async def on_file(path: str, written: bool) -> None:
            await r.remote_jobs.record_mcp_file(job.id, path, written)

        workspace = (self.workspace_root(swarm.id)
                     if files and self.workspace_root is not None else None)
        return AgentContext(repos=r, swarm=swarm, team=team, agent=agent, task=task,
                            embedder=self.embedder, signal=self.signal, workspace=workspace,
                            job_id=job.id, on_file=on_file if files else None), None

    async def call_tool(self, identity: Identity, name: str, arguments: dict) -> tuple[str, bool]:
        """(text, is_error) for one tools/call."""
        args = dict(arguments or {})
        requested = args.pop("job", None)
        if requested is not None and not isinstance(requested, str):
            return "Error: 'job' must be a DAASH job id (a string).", True
        job, problem = await self._job(identity, (requested or "").strip() or None)
        if problem:
            return problem, True
        ctx, problem = await self._context(identity, job)
        if problem:
            return problem, True
        registry = ToolRegistry(build_agent_tools(ctx, workspace=identity.file_tools))
        outcome = await registry.call(name, args)
        await self._record(identity, ctx, name, args, outcome.error)
        return outcome.output, outcome.error

    async def _record(self, identity: Identity, ctx: AgentContext, name: str, args: dict,
                      error: bool) -> None:
        """A `tool` event, the job's call count and the connected agent's last-seen time."""
        via = (f" ({identity.connected.name})" if identity.kind == "connected"
               else "")
        try:
            await self.repos.events.add(
                ctx.swarm.id, "tool", team_id=ctx.team.id, task_id=ctx.task.id,
                actor=ctx.agent.id,
                detail=f"{ctx.agent.name}{via} used DAASH tool {name}"
                       + (f" ({agent_tools.short_args(name, args)})" if args else "")
                       + (" - it failed" if error else "") + ".")
            await self.repos.remote_jobs.count_mcp_call(ctx.job_id)
            if identity.kind == "connected":
                await self.repos.connected.touch_mcp(identity.connected.id)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail the call
            log.warning("could not record MCP call %s: %r", name, exc)


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _result(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


class McpHandler:
    """MCP over JSON-RPC for one HTTP request. `backend` needs identify / list_tools /
    call_tool (see DaashBackend); tests may pass a stub."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    async def handle_http(self, token: str, body: bytes) -> tuple[int, Optional[Any]]:
        """(HTTP status, JSON body or None): 401 for a missing/unknown token, 202 when the
        message(s) needed no answer, else 200 with the response (or a batch's responses)."""
        identity = await self.backend.identify(token) if token else None
        if identity is None:
            return 401, {"detail": "Invalid or missing DAASH MCP token"}
        if len(body) > MAX_BODY_BYTES:
            return 413, _error(None, JSONRPC_INVALID_REQUEST, "request body too large")
        try:
            payload = json.loads(body or b"")
        except ValueError:
            return 400, _error(None, JSONRPC_PARSE_ERROR, "Parse error: the body is not JSON")
        if isinstance(payload, list):
            if not payload:
                return 400, _error(None, JSONRPC_INVALID_REQUEST, "Invalid Request: empty batch")
            out = [r for r in [await self.dispatch(identity, m) for m in payload] if r is not None]
            return (200, out) if out else (202, None)
        res = await self.dispatch(identity, payload)
        return (200, res) if res is not None else (202, None)

    async def dispatch(self, identity: Identity, msg: Any) -> Optional[dict]:
        """The response to one JSON-RPC message, or None (notifications and client responses)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            mid = msg.get("id") if isinstance(msg, dict) else None
            return _error(mid, JSONRPC_INVALID_REQUEST, "Invalid Request: not JSON-RPC 2.0")
        method = msg.get("method")
        if method is None:
            return None       # a response to a server request; DAASH never sends any
        if "id" not in msg:
            return None       # a notification (notifications/initialized, .../cancelled, ...)
        mid = msg["id"]
        if not isinstance(method, str):
            return _error(mid, JSONRPC_INVALID_REQUEST, "Invalid Request: method must be a string")
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(mid, JSONRPC_INVALID_PARAMS, "params must be an object")
        try:
            if method == "initialize":
                return _result(mid, {
                    "protocolVersion": negotiate_version(params.get("protocolVersion")),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": dict(SERVER_INFO),
                    "instructions": instructions(identity)})
            if method == "ping":
                return _result(mid, {})
            if method == "tools/list":
                return _result(mid, {"tools": self.backend.list_tools(identity)})
            if method == "tools/call":
                return await self._call(identity, mid, params)
            if method == "resources/list":
                return _result(mid, {"resources": []})
            if method == "resources/templates/list":
                return _result(mid, {"resourceTemplates": []})
            if method == "prompts/list":
                return _result(mid, {"prompts": []})
        except Exception:  # noqa: BLE001
            log.exception("MCP %s failed", method)
            return _error(mid, JSONRPC_INTERNAL_ERROR, "Internal error")
        return _error(mid, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {method}")

    async def _call(self, identity: Identity, mid: Any, params: dict) -> dict:
        name, arguments = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or not name:
            return _error(mid, JSONRPC_INVALID_PARAMS, "tools/call needs the tool's name")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(mid, JSONRPC_INVALID_PARAMS, "tools/call arguments must be an object")
        try:
            text, is_error = await self.backend.call_tool(identity, name, arguments)
        except Exception as exc:  # noqa: BLE001 - a tool problem is a result, not an HTTP error
            log.exception("MCP tool %s failed", name)
            text, is_error = f"Error: DAASH could not run {name} ({type(exc).__name__}).", True
        return _result(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})


# ---------------------------------------------------------------------------
# HTTP (mounted by app.main and app.agent_api)
# ---------------------------------------------------------------------------

def backend_for(state: Any) -> DaashBackend:
    """A backend on an app's process-wide handles (see app.api_state)."""
    from app import services
    from app.api_state import require_pool

    pool = require_pool(state)
    settings = state.settings
    return DaashBackend(Repos.from_pool(pool), embedder=getattr(state, "embedder", None),
                        signal=agent_tools.bus_signal(getattr(state, "bus", None)),
                        workspace_root=lambda swarm_id: services.ensure_workspace(settings,
                                                                                  swarm_id))


def build_router(state: Any) -> APIRouter:
    router = APIRouter()

    @router.post(MCP_PATH)
    async def mcp_post(request: Request) -> Response:
        handler = McpHandler(backend_for(state))
        status, body = await handler.handle_http(
            bearer_token(request.headers.get("authorization")), await request.body())
        if status == 401:
            return JSONResponse(body, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if body is None:
            return Response(status_code=status)
        return JSONResponse(body, status_code=status)

    @router.get(MCP_PATH)
    async def mcp_get() -> Response:
        """No server-to-client SSE stream: the server is stateless."""
        return Response(status_code=405, headers={"Allow": "POST"})

    @router.delete(MCP_PATH)
    async def mcp_delete() -> Response:
        """No sessions to terminate."""
        return Response(status_code=405, headers={"Allow": "POST"})

    return router
