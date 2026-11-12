"""Connected agents: what the admin API (app.connected_api) and the engine's approval watcher
share (docs/DESIGN-0.4.md sections 5.7 and 5.8): the API view of an agent, checks and their
bookkeeping, pending -> connected detection, and discovery with its stored results."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import asyncpg

from app import connectors
from app.connectors import ConnectorError, split_stored_error
from app.models import CONNECTED_HARNESS, DIRECT_PROTOCOLS, ConnectedAgent
from app.repo import (ConnectedAgentRepo, DiscoveryRepo, EventRepo, RunnerRepo, SwarmRepo)

log = logging.getLogger("daash.connected")

PROTOCOL_LABELS = {"a2a": "A2A", "hermes-api": "Hermes API", "runner": "agent host"}
KIND_LABELS = {"openclaw": "OpenClaw", "hermes": "Hermes Agent", "a2a": "A2A agent",
               "other": "agent"}
#: A check runs when the Agents page loads at most this often per agent (section 5.7).
AUTO_CHECK_SECONDS = 60.0
_last_auto_check: dict[str, float] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def label(ca: ConnectedAgent) -> str:
    """'Jane (A2A)', as delivery rows and roster badges show it."""
    return f"{ca.name} ({PROTOCOL_LABELS.get(ca.protocol, ca.protocol)})"


def availability_status(ca: ConnectedAgent, runner_online: Optional[bool] = None) -> str:
    """available | unreachable | token_rejected | disabled | pending | offline | unchecked."""
    if not ca.enabled:
        return "disabled"
    if ca.status == "pending":
        return "pending"
    if ca.status == "offline":
        return "offline"
    if ca.protocol == "runner":
        return "available" if runner_online is not False else "offline"
    kind, _ = split_stored_error(ca.last_error)
    if ca.last_error:
        return "token_rejected" if kind == "token_rejected" else "unreachable"
    return "available" if ca.last_ok_at is not None else "unchecked"


def agent_view(ca: ConnectedAgent, *, runner: Optional[dict] = None,
               now: Optional[datetime] = None) -> dict:
    """The API form of a connected agent: never the token DAASH sends nor the MCP token hash."""
    now = now or _now()
    d = ca.model_dump(mode="json")          # `token` and `mcp_token_hash` are excluded fields
    d.pop("token", None)
    d.pop("mcp_token_hash", None)
    kind, message = split_stored_error(ca.last_error)
    online = runner.get("online") if runner else None
    d.update({
        "label": label(ca),
        "kind_label": KIND_LABELS.get(ca.kind, ca.kind),
        "protocol_label": PROTOCOL_LABELS.get(ca.protocol, ca.protocol),
        "direct": ca.protocol in DIRECT_PROTOCOLS,
        "availability": availability_status(ca, online),
        "last_error": message, "error_kind": kind,
        "has_token": bool(ca.token),
        "pending_expired": bool(ca.status == "pending" and ca.pending_until is not None
                                and ca.pending_until <= now),
        "daash_tools": {"in_use": ca.mcp_seen_at is not None,
                        "last_call": ca.mcp_seen_at.isoformat() if ca.mcp_seen_at else None},
        "runner": ({"id": runner.get("id"), "name": runner.get("name"),
                    "online": runner.get("online")} if runner else None),
    })
    return d


async def check_and_record(pool: asyncpg.Pool, ca: ConnectedAgent, *,
                           transport: Any = None) -> tuple[ConnectedAgent, Optional[ConnectorError]]:
    """Run the cheap check of a direct agent and store the outcome (last_ok_at / last_error); a
    pending agent that passes becomes connected, with an event in the live swarm."""
    repo = ConnectedAgentRepo(pool)
    if ca.protocol not in DIRECT_PROTOCOLS:
        return ca, None
    _last_auto_check[ca.id] = time.monotonic()
    try:
        await connectors.check_agent(ca, transport=transport)
    except ConnectorError as exc:
        return (await repo.record_check(ca.id, False, exc.stored()) or ca), exc
    if ca.status == "pending":
        flipped = await repo.mark_connected(ca.id)
        if flipped is not None:
            await _event(pool, f"{ca.name} accepted DAASH's connection "
                               f"({KIND_LABELS.get(ca.kind, ca.kind)}, "
                               f"{PROTOCOL_LABELS.get(ca.protocol, ca.protocol)}).")
            return flipped, None
    return (await repo.record_check(ca.id, True) or ca), None


async def _event(pool: asyncpg.Pool, detail: str) -> None:
    """A `connected` event in the live swarm's feed, if there is a live swarm."""
    try:
        live = await SwarmRepo(pool).get_live()
        if live is not None:
            await EventRepo(pool).add(live.id, "info", actor="engine", detail=detail)
    except Exception as exc:  # noqa: BLE001 - an event is a courtesy
        log.debug("no event for %r: %r", detail, exc)


async def check_pending(pool: asyncpg.Pool, *, transport: Any = None,
                        now: Optional[datetime] = None) -> list[ConnectedAgent]:
    """One round of approval detection (section 5.8): check every pending direct agent inside
    its window; returns the agents that just became connected."""
    pending = [a for a in await ConnectedAgentRepo(pool).list_pending(now or _now())
               if a.protocol in DIRECT_PROTOCOLS]
    if not pending:
        return []
    results = await asyncio.gather(*(check_and_record(pool, a, transport=transport)
                                     for a in pending), return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, tuple) and r[1] is None and r[0].status == "connected":
            out.append(r[0])
    return out


async def auto_check(pool: asyncpg.Pool, agents: Iterable[ConnectedAgent], *,
                     transport: Any = None, budget: float = 6.0) -> None:
    """Check the enabled direct agents not checked in the last minute (the Agents page loaded),
    together and within `budget` seconds."""
    now = time.monotonic()
    due = [a for a in agents if a.enabled and a.protocol in DIRECT_PROTOCOLS
           and now - _last_auto_check.get(a.id, 0.0) >= AUTO_CHECK_SECONDS]
    if not due:
        return
    for a in due:
        _last_auto_check[a.id] = now
    try:
        await asyncio.wait_for(asyncio.gather(
            *(check_and_record(pool, a, transport=transport) for a in due),
            return_exceptions=True), budget)
    except asyncio.TimeoutError:
        log.info("connected agent checks did not finish within %.0fs", budget)


async def runner_index(pool: asyncpg.Pool, runners: Optional[list[dict]] = None) -> dict[str, dict]:
    if runners is None:
        from app.remote import ONLINE_SECONDS
        now = _now()
        runners = [{"id": r.id, "name": r.name, "host": r.host, "enabled": r.enabled,
                    "online": bool(r.enabled and r.last_seen_at is not None
                                   and (now - r.last_seen_at).total_seconds() <= ONLINE_SECONDS)}
                   for r in await RunnerRepo(pool).list()]
    return {r["id"]: r for r in runners}


async def availability_rows(pool: asyncpg.Pool, cfg: Any, runners: list[dict], *,
                            transport: Any = None, check: bool = True) -> list[dict]:
    """The connected agents for /availability (section 5.7): each with protocol, status,
    capabilities, whether it uses DAASH's tools, and whether this swarm allows it."""
    repo = ConnectedAgentRepo(pool)
    agents = await repo.list()
    if check:
        await auto_check(pool, agents, transport=transport)
        agents = await repo.list()
    by_runner = await runner_index(pool, runners)
    allowed = CONNECTED_HARNESS in cfg.allowed_harnesses
    out = []
    now = _now()
    for ca in agents:
        v = agent_view(ca, runner=by_runner.get(ca.runner_id) if ca.runner_id else None, now=now)
        rows = sum(1 for d in cfg.delivery_harnesses
                   if d.harness == CONNECTED_HARNESS and d.connected_agent == ca.id)
        v.update({"allowed": allowed and ca.enabled, "delivery_rows": rows})
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Discovery (section 5.8)
# ---------------------------------------------------------------------------

def _agent_origins(agents: Iterable[ConnectedAgent]) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for a in agents:
        if not a.url:
            continue
        try:
            addr = connectors.parse_address(a.url)
        except ValueError:
            continue
        port = addr.port or (443 if addr.scheme == "https" else 80)
        out[(addr.host.lower(), port)] = a.id
    return out


def discovery_view(row: dict, names: dict[str, str]) -> dict:
    card = row.get("card") or {}
    skills = []
    if isinstance(card, dict):
        for s in card.get("skills") or []:
            if isinstance(s, dict):
                skills.append(str(s.get("name") or s.get("id") or ""))
    out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()
           if k != "card"}
    out["skills"] = [s for s in skills if s]
    out["connected_agent_name"] = names.get(row.get("connected_agent_id") or "")
    out["connected"] = bool(row.get("connected_agent_id"))
    out["address"] = f"{row['host']}:{row['port']}"
    return out


async def store_findings(pool: asyncpg.Pool, findings: Iterable[connectors.Finding]) -> list[dict]:
    agents = await ConnectedAgentRepo(pool).list()
    origins = _agent_origins(agents)
    repo = DiscoveryRepo(pool)
    rows = []
    for f in findings:
        rows.append(await repo.upsert(
            f.host, f.port, f.kind, f.status, name=f.name, url=f.url, card=f.card,
            detail=f.detail, connected_agent_id=origins.get((f.host.lower(), f.port))))
    return rows


async def list_discoveries(pool: asyncpg.Pool) -> list[dict]:
    names = {a.id: a.name for a in await ConnectedAgentRepo(pool).list()}
    return [discovery_view(r, names) for r in await DiscoveryRepo(pool).list()]


async def discovery_hosts(pool: asyncpg.Pool, settings: Any,
                          extra: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """(hosts, problems): DAASH_DISCOVERY_HOSTS first (the path that works through Tailscale),
    then the hosts of known runners, then the addresses of DAASH_DISCOVERY_SUBNETS."""
    hosts = list(extra) + connectors.parse_list(settings.DISCOVERY_HOSTS)
    for r in await RunnerRepo(pool).list():
        if r.host and r.host not in hosts and r.enabled:
            hosts.append(r.host)
    subnet_hosts, problems = connectors.expand_subnets(settings.DISCOVERY_SUBNETS)
    hosts.extend(h for h in subnet_hosts if h not in hosts)
    return list(dict.fromkeys(h for h in hosts if h)), problems


async def run_discovery(pool: asyncpg.Pool, settings: Any, *, extra_hosts: Iterable[str] = (),
                        transport: Any = None, tcp_open: Any = None) -> dict:
    hosts, problems = await discovery_hosts(pool, settings, extra_hosts)
    ports = connectors.parse_ports(settings.DISCOVERY_PORTS)
    pairs = connectors.targets(hosts, ports)
    t0 = time.monotonic()
    findings = await connectors.discover(pairs, transport=transport, tcp_open=tcp_open)
    await store_findings(pool, findings)
    hints = []
    if not connectors.parse_list(settings.DISCOVERY_HOSTS) and not settings.DISCOVERY_SUBNETS:
        hints.append("Nothing to look at yet: set DAASH_DISCOVERY_HOSTS in .env to the machines "
                     "your agents run on (on a tailnet, scripts/tailnet-hosts.sh prints the "
                     "value), or use Add by address.")
    return {"discoveries": await list_discoveries(pool),
            "looked_at": {"hosts": hosts[:200], "host_count": len(hosts), "ports": ports,
                          "subnets": connectors.parse_list(settings.DISCOVERY_SUBNETS),
                          "problems": problems},
            "found": sum(1 for f in findings if f.status == "found"),
            "silent": sum(1 for f in findings if f.status == "silent"),
            "seconds": round(time.monotonic() - t0, 2), "hints": hints}


async def probe_address(pool: asyncpg.Pool, address: str, *, transport: Any = None,
                        tcp_open: Any = None, ports: Iterable[int] = connectors.DEFAULT_PORTS
                        ) -> list[connectors.Finding]:
    """'Add by address': a bare host name is tried on the default ports; host:port or a URL on
    its own port. Raises ValueError for something that is not an address."""
    addr = connectors.parse_address(address)
    if addr.scheme == "https" or addr.path:
        found = await _probe_url(addr, transport=transport)
        await store_findings(pool, found)
        return found
    pairs = connectors.targets([addr.host if addr.port is None else
                                f"{addr.host}:{addr.port}"], list(ports))
    found = await connectors.discover(pairs, transport=transport, tcp_open=tcp_open)
    await store_findings(pool, found)
    return found


async def _probe_url(addr: connectors.Address, *, transport: Any = None
                     ) -> list[connectors.Finding]:
    """An https URL or one with a path (e.g. a card URL behind a reverse proxy)."""
    url = addr.origin + addr.path
    port = addr.port or (443 if addr.scheme == "https" else 80)
    try:
        card, _ = await connectors.fetch_card(url, transport=transport)
        info = connectors.parse_card(card)
        return [connectors.Finding(addr.host, port, info.kind, "found", name=info.name,
                                   url=addr.origin, card=card,
                                   detail=connectors._card_detail(info))]
    except ConnectorError:
        pass
    try:
        health = await connectors.HermesClient(addr.origin, None, transport=transport).health()
    except ConnectorError:
        return []
    if connectors.looks_like_hermes_health(health):
        return [connectors.Finding(addr.host, port, "hermes", "found", name=f"Hermes on "
                                   f"{addr.host}", url=addr.origin, card={"health": health},
                                   detail="Hermes Agent API server")]
    return []


async def unique_name(pool: asyncpg.Pool, wanted: str, host: str = "") -> str:
    """`wanted`, or "wanted (host)", or "wanted 2", ... whichever is free (case-insensitive)."""
    repo = ConnectedAgentRepo(pool)
    base = " ".join((wanted or "").split())[:80] or "Agent"
    candidates = [base] + ([f"{base} ({host})"] if host else []) + [
        f"{base} {i}" for i in range(2, 50)]
    for c in candidates:
        if await repo.get_by_name(c) is None:
            return c
    return f"{base} {int(time.time())}"


def pending_until(settings: Any, now: Optional[datetime] = None) -> datetime:
    return (now or _now()) + timedelta(minutes=settings.CONNECTED_PENDING_MINUTES)
