"""Admin API for connected agents (docs/DESIGN-0.4.md sections 5.7 and 5.8), mounted on the admin
app only (app.main; DAASH_API_TOKEN like every admin route), never on the agent-facing port.

    GET    /connected-agents                       list (+ where agents reach DAASH)
    GET    /connected-agents/discoveries           what discovery found
    POST   /connected-agents/discover              look again (DAASH_DISCOVERY_* + runner hosts)
    POST   /connected-agents/probe                 "Add by address" (a bare host tries 18789, 8642)
    POST   /connected-agents                       connect: a pending agent + the owner's one step
    GET    /connected-agents/{id}                  one agent
    PATCH  /connected-agents/{id}                  name, enabled, capabilities, max_jobs, endpoint,
                                                   api_key (Hermes: the owner's own key)
    POST   /connected-agents/{id}/check            test now
    POST   /connected-agents/{id}/retry            wait another hour for the owner's approval
    POST   /connected-agents/{id}/reset-tokens     new tokens, back to pending, a new step
    DELETE /connected-agents/{id}                  remove

`token` (what DAASH sends) and the MCP token hash are never returned. The tokens of the approval
step are returned only by connect and reset-tokens, while the agent is pending.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from app import approval, connectors
from app import connected_service as CS
from app.api_state import agent_endpoint_info, require_pool
from app.connectors import ConnectorError
from app.mcp_server import new_connected_token
from app.models import (CONNECTED_KINDS, DIRECT_PROTOCOLS, ConnectedAgent, normalize_capabilities)
from app.remote import hash_token
from app.repo import ConnectedAgentRepo, DiscoveryRepo, RemoteJobRepo


class ConnectRequest(BaseModel):
    address: Optional[str] = Field(None, description="Agent Card URL, gateway or API origin, "
                                   "host:port, or a bare host name (default ports tried)")
    discovery_id: Optional[str] = None
    name: Optional[str] = Field(None, max_length=80)
    protocol: Optional[str] = None          # a2a | hermes-api (found out when omitted)
    kind: Optional[str] = None              # openclaw | hermes | a2a | other
    endpoint: Optional[str] = None          # A2A JSON-RPC URL override
    skill: Optional[str] = None
    api_key: Optional[str] = Field(None, max_length=500)   # Hermes: the owner's own key
    capabilities: Optional[list[str]] = None
    max_jobs: int = Field(1, ge=1, le=16)


class PatchRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    enabled: Optional[bool] = None
    capabilities: Optional[list[str]] = None
    max_jobs: Optional[int] = Field(None, ge=1, le=16)
    endpoint: Optional[str] = None
    api_key: Optional[str] = Field(None, min_length=8, max_length=500)


class DiscoverRequest(BaseModel):
    hosts: list[str] = Field(default_factory=list)


class ProbeRequest(BaseModel):
    address: str = Field(..., min_length=1, max_length=300)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_router(state: Any) -> APIRouter:
    """`state` gives `pool`, `settings` and `connector_transport` (None = real network)."""
    router = APIRouter()

    def pool() -> asyncpg.Pool:
        return require_pool(state)

    def transport() -> Any:
        return getattr(state, "connector_transport", None)

    async def view(ca: ConnectedAgent) -> dict:
        runners = await CS.runner_index(pool())
        return CS.agent_view(ca, runner=runners.get(ca.runner_id) if ca.runner_id else None)

    async def get_or_404(agent_id: str) -> ConnectedAgent:
        ca = await ConnectedAgentRepo(pool()).get(agent_id)
        if ca is None:
            raise HTTPException(404, "connected agent not found")
        return ca

    def endpoint_info() -> dict:
        return agent_endpoint_info(state.settings.AGENT_PUBLIC_URL)

    @router.get("/connected-agents")
    async def list_connected() -> dict:
        agents = await ConnectedAgentRepo(pool()).list()
        runners = await CS.runner_index(pool())
        return {"connected_agents": [CS.agent_view(a, runner=runners.get(a.runner_id)
                                                   if a.runner_id else None) for a in agents],
                "agent_endpoint": endpoint_info()}

    @router.get("/connected-agents/discoveries")
    async def get_discoveries() -> dict:
        return {"discoveries": await CS.list_discoveries(pool())}

    @router.post("/connected-agents/discover")
    async def discover(req: Optional[DiscoverRequest] = None) -> dict:
        extra = [h for h in (req.hosts if req else []) if h.strip()]
        return await CS.run_discovery(pool(), state.settings, extra_hosts=extra,
                                      transport=transport())

    @router.post("/connected-agents/probe")
    async def probe(req: ProbeRequest) -> dict:
        try:
            found = await CS.probe_address(pool(), req.address, transport=transport())
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        listed = await CS.list_discoveries(pool())
        keys = {(f.host, f.port) for f in found}
        return {"found": [d for d in listed if (d["host"], d["port"]) in keys],
                "address": req.address,
                "message": None if found else (
                    f"Nothing answered at {req.address} on the ports DAASH tried "
                    f"({', '.join(map(str, connectors.DEFAULT_PORTS))} for a bare host name). "
                    "Check the address and that the agent's gateway or API server listens on "
                    "the network (not only on 127.0.0.1); behind a firewall, use an agent host.")}

    @router.post("/connected-agents", status_code=201)
    async def connect(req: ConnectRequest) -> dict:
        return await _connect(req)

    async def _connect(req: ConnectRequest) -> dict:
        target_url, protocol, kind, card, host, disc_id = await _resolve_target(req)
        repo = ConnectedAgentRepo(pool())
        settings = state.settings
        endpoint = skill = None
        if protocol == "a2a":
            if card is None:
                # Before the owner's approval an OpenClaw gateway serves no Agent Card (its a2a
                # channel is off), so the card is optional: the endpoint is then the usual path.
                try:
                    card, _ = await connectors.fetch_card(target_url, transport=transport())
                except ConnectorError:
                    card = None
            info = connectors.parse_card(card) if card else connectors.CardInfo(
                name="", kind=kind if kind in ("openclaw", "a2a") else "a2a")
            endpoint = connectors.endpoint_for(target_url, info, req.endpoint)
            kind = req.kind or kind or info.kind
            skill = req.skill or (info.skills[0]["id"] if len(info.skills) == 1 else None)
            wanted = req.name or CS.default_name(info, kind, host)
            peer_token = approval.new_peer_token()
        else:
            kind = "hermes"
            wanted = req.name or f"Hermes ({host})"
            peer_token = (req.api_key or "").strip() or approval.new_api_key()
        if kind not in CONNECTED_KINDS:
            kind = "other"
        name = await CS.unique_name(pool(), wanted, host)
        mcp_token = new_connected_token()
        caps = normalize_capabilities(req.capabilities) if req.capabilities is not None else None
        try:
            ca = await repo.create(
                name, protocol, target_url, endpoint=endpoint, skill=skill, token=peer_token,
                mcp_token_hash=hash_token(mcp_token), capabilities=caps,
                max_jobs=req.max_jobs, card=card, status="pending", kind=kind,
                pending_until=CS.pending_until(settings))
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, f"A connected agent named '{name}' already exists.")
        if disc_id:
            await DiscoveryRepo(pool()).link(disc_id, ca.id)
        return {"agent": await view(ca),
                "approval": approval.approval_step(ca, peer_token, mcp_token,
                                                   settings.AGENT_PUBLIC_URL),
                "agent_endpoint": endpoint_info()}

    async def _resolve_target(req: ConnectRequest
                              ) -> tuple[str, str, Optional[str], Optional[dict], str,
                                         Optional[str]]:
        """(url, protocol, kind, card, host, discovery id) of what the user wants to connect."""
        if req.protocol and req.protocol not in DIRECT_PROTOCOLS:
            raise HTTPException(422, "protocol must be 'a2a' or 'hermes-api' (agents behind a "
                                     "firewall connect through an agent host instead)")
        if req.discovery_id:
            d = await DiscoveryRepo(pool()).get(req.discovery_id)
            if d is None:
                raise HTTPException(404, "discovery result not found; look again")
            if d["status"] != "found" or not d.get("url"):
                raise HTTPException(422, f"{d['host']}:{d['port']} does not answer, so DAASH "
                                         "cannot connect to it directly; connect it through an "
                                         "agent host instead.")
            protocol = "hermes-api" if d["kind"] == "hermes" else "a2a"
            return (d["url"], req.protocol or protocol, d["kind"],
                    d.get("card") if protocol == "a2a" else None, d["host"], d["id"])
        if not req.address:
            raise HTTPException(422, "give an address or a discovery_id")
        try:
            addr = connectors.parse_address(req.address)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if req.protocol:
            # the owner has not turned anything on yet: connect to the usual port without
            # probing (the approval step is what makes the agent answer)
            if addr.port is None and not addr.path and addr.scheme == "http":
                addr = addr.with_port(connectors.HERMES_PORT if req.protocol == "hermes-api"
                                      else connectors.OPENCLAW_PORT)
            return (addr.origin + addr.path, req.protocol,
                    "hermes" if req.protocol == "hermes-api" else req.kind, None, addr.host,
                    None)
        found = await CS.probe_address(pool(), req.address, transport=transport())
        hits = [f for f in found if f.status == "found"
                and (req.protocol is None or (f.kind == "hermes") == (req.protocol == "hermes-api"))]
        if not hits:
            silent = [f for f in found if f.status == "silent"]
            if silent:
                raise HTTPException(422, f"{req.address}: {connectors.SILENT_DETAIL}")
            raise HTTPException(422, f"No agent answered at {req.address}. A bare host name is "
                                     f"tried on ports {', '.join(map(str, connectors.DEFAULT_PORTS))}"
                                     "; give host:port or the Agent Card URL otherwise. A Hermes "
                                     "API server that is not enabled yet does not answer: choose "
                                     "Hermes (protocol hermes-api) to get its approval step.")
        f = hits[0]
        url = f.url if not addr.path or addr.path.endswith(".json") else addr.origin + addr.path
        rows = await DiscoveryRepo(pool()).list()
        disc = next((r for r in rows if r["host"] == f.host and r["port"] == f.port), None)
        protocol = "hermes-api" if f.kind == "hermes" else "a2a"
        return (url or addr.origin, protocol, f.kind, f.card if protocol == "a2a" else None,
                f.host, disc["id"] if disc else None)

    @router.get("/connected-agents/{agent_id}")
    async def get_connected(agent_id: str) -> dict:
        return await view(await get_or_404(agent_id))

    @router.patch("/connected-agents/{agent_id}")
    async def patch_connected(agent_id: str, req: PatchRequest) -> dict:
        ca = await get_or_404(agent_id)
        fields: dict[str, Any] = {}
        if req.name is not None and req.name.strip() != ca.name:
            other = await ConnectedAgentRepo(pool()).get_by_name(req.name.strip())
            if other is not None and other.id != ca.id:
                raise HTTPException(409, f"A connected agent named '{req.name.strip()}' "
                                         "already exists.")
            fields["name"] = req.name.strip()
        if req.enabled is not None:
            fields["enabled"] = req.enabled
        if req.capabilities is not None:
            fields["capabilities"] = normalize_capabilities(req.capabilities)
        if req.max_jobs is not None:
            fields["max_jobs"] = req.max_jobs
        if req.endpoint is not None:
            if ca.protocol != "a2a":
                raise HTTPException(422, "only A2A agents have an endpoint")
            try:
                connectors.parse_address(req.endpoint)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            fields["endpoint"] = req.endpoint.strip()
        if req.api_key is not None:
            if ca.protocol != "hermes-api":
                raise HTTPException(422, "only Hermes agents take an API key; A2A tokens are "
                                         "made by DAASH (reset tokens to get new ones)")
            fields["token"] = req.api_key.strip()
            fields["last_error"] = None
        if not fields:
            return await view(ca)
        updated = await ConnectedAgentRepo(pool()).update(agent_id, **fields)
        if req.enabled is False:
            await _cancel_jobs(agent_id)
        return await view(updated or ca)

    @router.post("/connected-agents/{agent_id}/check")
    async def check_connected(agent_id: str) -> dict:
        ca = await get_or_404(agent_id)
        if ca.protocol not in DIRECT_PROTOCOLS:
            v = await view(ca)
            return {"agent": v, "ok": v["availability"] == "available",
                    "error_kind": None if v["availability"] == "available" else "offline",
                    "error": None if v["availability"] == "available"
                    else "its agent host's runner is offline"}
        updated, exc = await CS.check_and_record(pool(), ca, transport=transport())
        return {"agent": await view(updated), "ok": exc is None,
                "error_kind": exc.kind if exc else None, "error": str(exc) if exc else None}

    @router.post("/connected-agents/{agent_id}/retry")
    async def retry_connected(agent_id: str) -> dict:
        ca = await get_or_404(agent_id)
        if ca.status != "pending":
            raise HTTPException(409, f"{ca.name} is not waiting for approval")
        updated = await ConnectedAgentRepo(pool()).update(
            agent_id, pending_until=CS.pending_until(state.settings))
        return await view(updated or ca)

    @router.post("/connected-agents/{agent_id}/reset-tokens")
    async def reset_tokens(agent_id: str) -> dict:
        ca = await get_or_404(agent_id)
        if ca.protocol not in DIRECT_PROTOCOLS:
            raise HTTPException(422, "an agent host's agents get their tokens from the runner; "
                                     "reconnect the agent host instead")
        peer = (approval.new_api_key() if ca.protocol == "hermes-api"
                else approval.new_peer_token())
        mcp_token = new_connected_token()
        await _cancel_jobs(agent_id)
        updated = await ConnectedAgentRepo(pool()).update(
            agent_id, token=peer, mcp_token_hash=hash_token(mcp_token), status="pending",
            pending_until=CS.pending_until(state.settings), last_error=None)
        return {"agent": await view(updated or ca),
                "approval": approval.approval_step(updated or ca, peer, mcp_token,
                                                   state.settings.AGENT_PUBLIC_URL),
                "agent_endpoint": endpoint_info()}

    @router.delete("/connected-agents/{agent_id}", status_code=204)
    async def delete_connected(agent_id: str) -> Response:
        await get_or_404(agent_id)
        await _cancel_jobs(agent_id)
        await ConnectedAgentRepo(pool()).delete(agent_id)
        return Response(status_code=204)

    async def _cancel_jobs(agent_id: str) -> None:
        """Its active jobs end (their MCP calls are refused); the engine then runs those
        attempts on the built-in agent or retries."""
        repo = RemoteJobRepo(pool())
        for j in await repo.active_connected(agent_id):
            await repo.cancel(j.id)

    return router
