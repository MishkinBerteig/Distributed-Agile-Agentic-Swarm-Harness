"""Process-wide handles shared by the routers that both HTTP apps mount (docs/DESIGN-0.4.md
section 6): the admin API (app.main, port 8000) and the agent-facing API (app.agent_api, 8001).

Each app keeps `pool`, `settings`, `bus` and `embedder` as module globals set by its lifespan.
`ModuleState` reads them at request time, so the runner and MCP routers work on either app and
tests can still replace an app's globals (e.g. monkeypatch app.main.pool)."""
from __future__ import annotations

import sys
from typing import Any, Optional

from fastapi import HTTPException

#: Port of the agent-facing API (compose service `agent-api`).
AGENT_PORT = 8001


class ModuleState:
    """Attribute access to another module's globals, resolved on every access."""

    def __init__(self, module_name: str) -> None:
        self._module_name = module_name

    def __getattr__(self, name: str) -> Any:
        return getattr(sys.modules[self._module_name], name)


def require_pool(state: Any):
    pool = getattr(state, "pool", None)
    if pool is None:
        raise HTTPException(503, "database not ready")
    return pool


def agent_endpoint_info(public_url: Optional[str]) -> dict:
    """Where agents on other machines reach DAASH (DAASH_AGENT_PUBLIC_URL): the base URL, the
    MCP URL for connected agents' config snippets, and how to set it when it is missing."""
    base = (public_url or "").strip().rstrip("/")
    return {
        "public_url": base or None,
        "mcp_url": f"{base}/mcp" if base else None,
        "runner_url": base or None,
        "port": AGENT_PORT,
        "configured": bool(base),
        "how_to_set": ("Set DAASH_AGENT_PUBLIC_URL in .env to the address other machines use "
                       f"for this one's agent port (for example http://<tailscale-name>:{AGENT_PORT}),"
                       " set DAASH_AGENT_BIND_ADDR (for example 0.0.0.0 or this machine's "
                       "Tailscale IP) so port 8001 is published beyond localhost, then restart "
                       "with `docker compose up -d`."),
    }
