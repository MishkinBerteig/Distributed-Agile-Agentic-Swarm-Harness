"""Giving the owner's agents DAASH's tools, and taking them away again (docs/DESIGN-0.4.md 5.9).

Each agent gets the DAASH MCP server, named `daash`, with its own connected-agent token (`dca_...`,
from the coordinator's hello answer), through the agent's own CLI:

* OpenClaw: `openclaw mcp add daash --url <coordinator>/mcp --transport streamable-http --header
  "Authorization=Bearer <token>"` (OpenClaw probes the server before saving; a running gateway
  picks the server up on the next turn). OpenClaw keeps MCP servers for all its agents in one
  place, so one OpenClaw install has one `daash` server: it goes to the default agent (or the first
  shared one). `openclaw mcp unset daash` removes it.
* Hermes Agent: in the profile's `config.yaml`, `mcp_servers.daash.url` and
  `mcp_servers.daash.headers.Authorization: "Bearer ${DAASH_MCP_TOKEN}"`, written with the owner's
  own `hermes config set` (which keeps the file's comments and layout), and the token itself as
  `DAASH_MCP_TOKEN` in the profile's `.env` (mode 0600). `hermes config unset mcp_servers.daash`
  and removing that `.env` line undo it.

A `daash` server that points somewhere else is never overwritten or removed.

`install_*` return (ok, message, retry): retry is True when the failure may be temporary (DAASH or
the CLI did not answer), False when it will not change by itself (another `daash` server exists).
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from .discovery import Runner, call, first_json

SERVER = "daash"
TOKEN_ENV = "DAASH_MCP_TOKEN"
ENV_MARKER = "# Added by the DAASH runner (python3 ~/daash-runner.pyz disconnect removes it)"


def mcp_url(coordinator: str) -> str:
    return coordinator.rstrip("/") + "/mcp"


# ---------------------------------------------------------------------------
# OpenClaw
# ---------------------------------------------------------------------------

def openclaw_current_url(cli: str, env: Mapping[str, str], run: Optional[Runner] = None
                         ) -> Optional[str]:
    """The URL of OpenClaw's `daash` MCP server, '' if it has no URL, None if there is none."""
    code, out, _ = call([cli, "mcp", "show", SERVER, "--json"], env, run=run)
    if code != 0:
        return None
    data = first_json(out, "{")
    if not isinstance(data, dict) or data.get("ok") is False:
        return None
    return str(data.get("url") or "")


def openclaw_add_argv(cli: str, url: str, token: str) -> list:
    return [cli, "mcp", "add", SERVER, "--url", url, "--transport", "streamable-http",
            "--header", f"Authorization=Bearer {token}"]


def install_openclaw(cli: str, env: Mapping[str, str], url: str, token: str,
                     run: Optional[Runner] = None) -> tuple:
    """(ok, message). Replaces a `daash` server only when it already points at this DAASH."""
    current = openclaw_current_url(cli, env, run)
    if current is not None:
        if current.rstrip("/") != url.rstrip("/"):
            return False, (f"OpenClaw already has an MCP server named '{SERVER}' ({current or 'no url'});"
                           " left it unchanged, so this agent works without DAASH tools"), False
        call([cli, "mcp", "unset", SERVER], env, run=run)
    code, out, err = call(openclaw_add_argv(cli, url, token), env, run=run)
    if code != 0:
        return False, f"`openclaw mcp add {SERVER}` failed: {_short(err or out)}", True
    return True, f"added MCP server '{SERVER}' ({url}) to OpenClaw's config", False


def remove_openclaw(cli: str, env: Mapping[str, str], url: str,
                    run: Optional[Runner] = None) -> tuple:
    """(ok, message). Only removes a `daash` server that points at this DAASH."""
    current = openclaw_current_url(cli, env, run)
    if current is None:
        return True, f"OpenClaw has no MCP server '{SERVER}' (nothing to remove)"
    if current.rstrip("/") != url.rstrip("/"):
        return True, f"OpenClaw's MCP server '{SERVER}' points at {current}, not this DAASH; left it"
    code, out, err = call([cli, "mcp", "unset", SERVER], env, run=run)
    if code != 0:
        return False, f"`openclaw mcp unset {SERVER}` failed: {_short(err or out)}"
    return True, f"removed MCP server '{SERVER}' from OpenClaw's config"


# ---------------------------------------------------------------------------
# Hermes Agent
# ---------------------------------------------------------------------------

def hermes_env(env: Mapping[str, str], home: str) -> dict:
    out = dict(env)
    out["HERMES_HOME"] = home
    return out


def hermes_current_url(cli: str, env: Mapping[str, str], home: str,
                       run: Optional[Runner] = None) -> Optional[str]:
    code, out, _ = call([cli, "config", "get", f"mcp_servers.{SERVER}.url"], hermes_env(env, home),
                        run=run)
    if code != 0:
        return None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def add_env_line(path: str, name: str, value: str) -> None:
    """Set `name=value` in a dotenv file (created 0600), with the DAASH marker comment above it.
    An earlier DAASH line for `name` is replaced; the rest of the file is kept as it is."""
    remove_env_line(path, name)
    existed = os.path.exists(path)
    text = ""
    if existed:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    if text and not text.endswith("\n"):
        text += "\n"
    text += f"{ENV_MARKER}\n{name}={value}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    if not existed:
        os.chmod(path, 0o600)


def remove_env_line(path: str, name: str) -> bool:
    """Remove `name=...` lines and the DAASH marker comment from a dotenv file. True if changed."""
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines(keepends=True)
    keep = [ln for ln in lines if ln.rstrip("\r\n") != ENV_MARKER
            and not ln.lstrip().startswith(f"{name}=")
            and not ln.lstrip().startswith(f"export {name}=")]
    if keep == lines:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(keep))
    return True


def install_hermes(cli: str, env: Mapping[str, str], home: str, url: str, token: str,
                   run: Optional[Runner] = None) -> tuple:
    current = hermes_current_url(cli, env, home, run)
    if current and current.rstrip("/") != url.rstrip("/"):
        return False, (f"this Hermes profile already has an MCP server named '{SERVER}' ({current}); "
                       "left it unchanged, so it works without DAASH tools"), False
    henv = hermes_env(env, home)
    for key, value in ((f"mcp_servers.{SERVER}.url", url),
                       (f"mcp_servers.{SERVER}.headers.Authorization", "Bearer ${%s}" % TOKEN_ENV)):
        code, out, err = call([cli, "config", "set", key, value], henv, run=run)
        if code != 0:
            return False, f"`hermes config set {key}` failed: {_short(err or out)}", True
    try:
        add_env_line(os.path.join(home, ".env"), TOKEN_ENV, token)
    except OSError as exc:
        return False, f"could not write {os.path.join(home, '.env')}: {exc}", True
    return True, (f"added mcp_servers.{SERVER} to {os.path.join(home, 'config.yaml')} and "
                  f"{TOKEN_ENV} to {os.path.join(home, '.env')}"), False


def remove_hermes(cli: str, env: Mapping[str, str], home: str, url: str,
                  run: Optional[Runner] = None) -> tuple:
    msgs, ok = [], True
    current = hermes_current_url(cli, env, home, run)
    if current is None:
        msgs.append(f"no mcp_servers.{SERVER} in {os.path.join(home, 'config.yaml')}")
    elif current.rstrip("/") != url.rstrip("/"):
        msgs.append(f"mcp_servers.{SERVER} points at {current}, not this DAASH; left it")
    else:
        code, out, err = call([cli, "config", "unset", f"mcp_servers.{SERVER}"],
                              hermes_env(env, home), run=run)
        if code != 0:
            ok = False
            msgs.append(f"`hermes config unset mcp_servers.{SERVER}` failed: {_short(err or out)}")
        else:
            msgs.append(f"removed mcp_servers.{SERVER} from {os.path.join(home, 'config.yaml')}")
    try:
        if remove_env_line(os.path.join(home, ".env"), TOKEN_ENV):
            msgs.append(f"removed {TOKEN_ENV} from {os.path.join(home, '.env')}")
    except OSError as exc:
        ok = False
        msgs.append(f"could not edit {os.path.join(home, '.env')}: {exc}")
    return ok, "; ".join(msgs)


def _short(text: str, limit: int = 400) -> str:
    """A CLI's error output on one line, at most `limit` characters (the end is kept)."""
    flat = " ".join((text or "").split())
    return flat[-limit:] if flat else "no output"
