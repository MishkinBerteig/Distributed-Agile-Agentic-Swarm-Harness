"""The owner's one approval step for a directly connected agent (docs/DESIGN-0.4.md section 5.8).

DAASH generates everything; the owner only pastes it. Pure: no I/O.

* OpenClaw: one `openclaw config patch --stdin` heredoc that enables the `a2a` channel, adds
  DAASH as the peer `daash` with its own bearer token, and adds DAASH's MCP server (`daash`) with
  the agent's `dca_` token. The same patch can instead be sent to the agent as a message, for an
  agent that may change its own configuration for its owner.
* Hermes Agent: `hermes config set` commands that give its API server a key and a network
  address (both land in the profile's `.env`) and add `mcp_servers.daash` to its
  `config.yaml`, then `hermes gateway restart` (the API server binds only at start; verified on
  v0.23.1, where any valid API_SERVER_KEY of 16+ characters turns the server on, the default
  host is 127.0.0.1, and the MCP entry alone would hot-reload within a minute).
* Any other A2A agent: what to configure, in words (A2A has no standard config format).
"""
from __future__ import annotations

import json
import secrets
from typing import Any, Optional

from app.connectors import HERMES_PORT

A2A_PEER = "daash"                     # DAASH's peer name in the agent's A2A config
MCP_SERVER = "daash"                   # DAASH's MCP server name in the agent's config
HEREDOC_MARK = "DAASH_PATCH"
#: A config patch on a running OpenClaw gateway takes effect without a restart (hot reload,
#: about a second; verified against OpenClaw 2026.11.3); kept as data so the snippet says so
#: when a version needs one.
OPENCLAW_RESTART: Optional[str] = None
#: Only when the owner turned config hot reload off (gateway.reload.mode "off") does the patch
#: say "Restart the gateway to apply"; then this restarts it (verified on 2026.11.3).
OPENCLAW_RELOAD_OFF_NOTE = ("If the command says \"Restart the gateway to apply\" (config "
                            "reload is turned off on this gateway), run `openclaw gateway "
                            "restart` once.")
HERMES_RESTART = "hermes gateway restart"


def new_peer_token() -> str:
    """The bearer DAASH sends to an A2A agent (`channels.a2a.peers.daash.token`)."""
    return "daash_a2a_" + secrets.token_urlsafe(32)


def new_api_key() -> str:
    """A key for a Hermes API server (API_SERVER_KEY), suggested by DAASH."""
    return "daash_hermes_" + secrets.token_urlsafe(32)


def mcp_url(public_url: Optional[str]) -> str:
    base = (public_url or "").strip().rstrip("/")
    return f"{base}/mcp" if base else "http://<this-machine>:8001/mcp"


def openclaw_patch(peer_token: str, mcp_token: str, public_url: Optional[str]) -> dict:
    """The JSON5 object for `openclaw config patch` (objects merge; nothing else changes)."""
    return {
        "plugins": {"entries": {"a2a": {"enabled": True}}},
        "channels": {"a2a": {"enabled": True,
                             "peers": {A2A_PEER: {"token": peer_token}}}},
        "mcp": {"servers": {MCP_SERVER: {
            "url": mcp_url(public_url), "transport": "streamable-http",
            "headers": {"Authorization": f"Bearer {mcp_token}"}}}},
    }


def openclaw_command(peer_token: str, mcp_token: str, public_url: Optional[str]) -> str:
    patch = json.dumps(openclaw_patch(peer_token, mcp_token, public_url), indent=2)
    cmd = f"openclaw config patch --stdin <<'{HEREDOC_MARK}'\n{patch}\n{HEREDOC_MARK}"
    if OPENCLAW_RESTART:
        cmd += f"\n{OPENCLAW_RESTART}"
    return cmd


def openclaw_message(agent_name: str, peer_token: str, mcp_token: str,
                     public_url: Optional[str]) -> str:
    patch = json.dumps(openclaw_patch(peer_token, mcp_token, public_url), indent=2)
    then = (f" Then run `{OPENCLAW_RESTART}`." if OPENCLAW_RESTART else "")
    return (f"Hi {agent_name}, please connect yourself to DAASH, my agent swarm, so it can give "
            "you tasks. Apply this patch to your OpenClaw configuration exactly as it is (for "
            f"example with `openclaw config patch --stdin`).{then} It turns on your A2A channel "
            f"with DAASH as the trusted peer \"{A2A_PEER}\" and adds DAASH's tools as the MCP "
            f"server \"{MCP_SERVER}\". Do not change anything else, and tell me when it is "
            f"done.\n\n```json5\n{patch}\n```")


def hermes_env(api_key: str, port: int = HERMES_PORT) -> str:
    return ("API_SERVER_ENABLED=true\n"
            f"API_SERVER_KEY={api_key}\n"
            "API_SERVER_HOST=0.0.0.0\n"
            f"API_SERVER_PORT={port}")


def hermes_mcp_yaml(mcp_token: str, public_url: Optional[str]) -> str:
    return (f"mcp_servers:\n"
            f"  {MCP_SERVER}:\n"
            f"    url: \"{mcp_url(public_url)}\"\n"
            f"    headers:\n"
            f"      Authorization: \"Bearer {mcp_token}\"\n"
            f"    timeout: 120\n"
            f"    connect_timeout: 15")


def hermes_mcp_json(mcp_token: str, public_url: Optional[str]) -> str:
    """The `mcp_servers.daash` value for `hermes config set` (a JSON literal it parses)."""
    return json.dumps({"url": mcp_url(public_url),
                       "headers": {"Authorization": f"Bearer {mcp_token}"},
                       "timeout": 120, "connect_timeout": 20})


def hermes_command(api_key: str, mcp_token: str, public_url: Optional[str],
                   port: int = HERMES_PORT) -> str:
    """One paste for the Hermes owner, in the profile Hermes uses (no file paths to guess, no
    duplicate keys): the API server's key, host and port go to its .env, DAASH's MCP server to
    its config.yaml, then the gateway restarts so the API server listens on the network."""
    return (f"hermes config set API_SERVER_KEY {api_key}\n"
            f"hermes config set API_SERVER_HOST 0.0.0.0\n"
            f"hermes config set API_SERVER_PORT {port}\n"
            f"hermes config set mcp_servers.{MCP_SERVER} '{hermes_mcp_json(mcp_token, public_url)}'\n"
            f"{HERMES_RESTART}")


def hermes_message(agent_name: str, api_key: str, mcp_token: str,
                   public_url: Optional[str]) -> str:
    return (f"Hi {agent_name}, please connect yourself to DAASH, my agent swarm, so it can give "
            "you tasks. Run these commands exactly as they are; they turn on your API server for "
            "DAASH and add DAASH's tools as the MCP server \"daash\". The last one restarts your "
            "gateway, so tell me first that you are about to do it, then that it is done.\n\n"
            f"```\n{hermes_command(api_key, mcp_token, public_url)}\n```")


def a2a_instructions(peer_token: str, mcp_token: str, public_url: Optional[str]) -> str:
    return (f"Configure the agent to accept A2A requests from DAASH with the bearer token\n"
            f"  {peer_token}\n"
            f"and give it DAASH's MCP server (streamable HTTP) at\n"
            f"  {mcp_url(public_url)}\n"
            f"with the header\n  Authorization: Bearer {mcp_token}")


def approval_step(agent: Any, peer_token: Optional[str], mcp_token: str,
                  public_url: Optional[str]) -> dict:
    """What DAASHboard shows the owner: a command to paste on the agent's machine (`command`,
    with `restart` when needed), a message to send the agent instead (`message`), the parts
    (`env`, `mcp_yaml`, `patch`), the tokens, and warnings."""
    warnings = []
    if not (public_url or "").strip():
        warnings.append("DAASH_AGENT_PUBLIC_URL is not set, so the MCP address in this step is a "
                        "placeholder: set it to the address the agent's machine uses for DAASH's "
                        "agent port (for example http://<tailscale-name>:8001), restart DAASH, "
                        "then reset this agent's tokens to get a corrected step.")
    out: dict[str, Any] = {"kind": agent.kind, "protocol": agent.protocol,
                           "mcp_url": mcp_url(public_url), "mcp_token": mcp_token,
                           "warnings": warnings, "restart": None}
    if agent.protocol == "hermes-api":
        out.update({
            "api_key": peer_token,
            "env": hermes_env(peer_token or "<your key>"),
            "mcp_yaml": hermes_mcp_yaml(mcp_token, public_url),
            "command": hermes_command(peer_token or "<your key>", mcp_token, public_url),
            "restart": HERMES_RESTART,
            "message": hermes_message(agent.name, peer_token or "<your key>", mcp_token,
                                      public_url),
            "where": "on the machine where Hermes Agent runs, as its owner",
            "note": "If the restart says live sessions would be interrupted, wait for them or "
                    "run `hermes gateway restart --yes`. To use a key of your own, use it in "
                    "API_SERVER_KEY (16 characters or more) and paste the same key into DAASH."})
        return out
    out["a2a_token"] = peer_token
    if agent.kind == "openclaw":
        out.update({
            "patch": openclaw_patch(peer_token or "", mcp_token, public_url),
            "command": openclaw_command(peer_token or "", mcp_token, public_url),
            "restart": OPENCLAW_RESTART,
            "message": openclaw_message(agent.name, peer_token or "", mcp_token, public_url),
            "where": "on the machine where the OpenClaw gateway runs, as its owner",
            "note": OPENCLAW_RELOAD_OFF_NOTE})
        return out
    text = a2a_instructions(peer_token or "", mcp_token, public_url)
    out.update({"command": text, "message": f"Hi {agent.name}, please connect yourself to "
                f"DAASH, my agent swarm.\n{text}", "where": "in the agent's own settings"})
    return out
