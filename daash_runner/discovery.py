"""Finding the owner's agents on an agent host (docs/DESIGN-0.4.md 5.9). Standard library only.

* OpenClaw: `openclaw agents list --json` gives each agent's id, identity name, workspace and
  whether it is the default agent; the gateway's health is read on loopback
  (`GET http://127.0.0.1:<gateway.port>/health` -> `{"ok": true, "status": "live"}`), because
  relayed turns go through the owner's running gateway.
* Hermes Agent: the default profile (`$HERMES_HOME`, default ~/.hermes) and the named profiles
  `hermes profile list` shows (each lives in `<home>/profiles/<name>`).

Each agent is reported to DAASH as {"kind", "ref", "name", "available", "detail"} (plus "tools":
whether it has DAASH's tools). `share_agents` in the runner config limits what is offered.
Output formats were captured from OpenClaw 2026.10.9 and Hermes Agent 0.23.1
(tests/fixtures/agent_hosts/).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

from . import hostenv

OPENCLAW = "openclaw"
HERMES = "hermes"
KINDS = (OPENCLAW, HERMES)
RELAY_OF = {OPENCLAW: "openclaw-agent", HERMES: "hermes-agent"}
KIND_OF_RELAY = {v: k for k, v in RELAY_OF.items()}
DEFAULT_GATEWAY_PORT = 18789
CLI_TIMEOUT = 90.0

Runner = Callable[..., "subprocess.CompletedProcess"]


@dataclass
class LocalAgent:
    kind: str                       # openclaw | hermes
    ref: str                        # OpenClaw agent id | Hermes profile name ("default")
    name: str
    available: bool = True
    detail: str = ""
    cli: str = ""                   # absolute path of the CLI
    workspace: str = ""             # OpenClaw: the agent's workspace (job folders go inside)
    home: str = ""                  # Hermes: the profile's HERMES_HOME
    is_default: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.ref}"

    @property
    def relay(self) -> str:
        return RELAY_OF[self.kind]

    def report(self, tools: bool = False, new_token: bool = False) -> dict:
        d = {"kind": self.kind, "ref": self.ref, "name": self.name, "available": self.available,
             "detail": self.detail, "tools": tools}
        if new_token:
            d["new_token"] = True
        return d


def call(argv: list, env: Optional[Mapping[str, str]] = None, timeout: float = CLI_TIMEOUT,
         run: Optional[Runner] = None, stdin: Optional[str] = None) -> tuple:
    """(exit code, stdout, stderr) of a CLI; (127, '', reason) when it cannot be started."""
    run = run or subprocess.run
    try:
        res = run(argv, env=dict(env) if env is not None else None, input=stdin,  # noqa: S603
                  capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "", f"{argv[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{os.path.basename(argv[0])} did not answer within {int(timeout)} s"
    except OSError as exc:
        return 126, "", str(exc)
    return res.returncode, res.stdout or "", res.stderr or ""


def first_json(text: str, opener: str = "[") -> Any:
    """The first JSON value starting with `opener` in a CLI's stdout (banners and warnings
    before it are skipped); None when there is none."""
    dec = json.JSONDecoder()
    for m in re.finditer(re.escape(opener), text or ""):
        try:
            value, _ = dec.raw_decode(text[m.start():])
            return value
        except ValueError:
            continue
    return None


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
# OpenClaw
# ---------------------------------------------------------------------------

def parse_openclaw_agents(stdout: str) -> list:
    """`openclaw agents list --json` -> [{"id", "name", "workspace", "is_default", "model"}]."""
    data = first_json(stdout, "[")
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        name = item.get("identityName") or item.get("name") or item["id"]
        out.append({"id": item["id"], "name": str(name).strip() or item["id"],
                    "workspace": str(item.get("workspace") or ""),
                    "is_default": bool(item.get("isDefault")),
                    "model": str(item.get("model") or "")})
    return out


def gateway_health(port: int, timeout: float = 3.0,
                   opener: Optional[Callable[..., Any]] = None) -> tuple:
    """(live, reason) for the OpenClaw gateway on 127.0.0.1:<port>; no credentials needed."""
    url = f"http://127.0.0.1:{port}/health"
    try:
        with (opener or urllib.request.urlopen)(url, timeout=timeout) as resp:  # noqa: S310
            body = json.loads(resp.read() or b"{}")
    except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
        return False, f"its gateway is not reachable on 127.0.0.1:{port} ({type(exc).__name__}); " \
                      "start it with `openclaw gateway start`"
    if isinstance(body, dict) and body.get("ok") is True:
        return True, f"gateway live on 127.0.0.1:{port}"
    return False, f"its gateway on 127.0.0.1:{port} is not healthy ({str(body)[:80]})"


def openclaw_config_get(cli: str, key: str, env: Mapping[str, str],
                        run: Optional[Runner] = None) -> str:
    """`openclaw config get <key>` ('' when unset or on error)."""
    code, out, _ = call([cli, "config", "get", key], env, run=run)
    if code != 0 or "valid but unset" in out:
        return ""
    return first_line(out)


def discover_openclaw(cli: str, env: Mapping[str, str], run: Optional[Runner] = None,
                      health: Optional[Callable[[int], tuple]] = None) -> tuple:
    """([LocalAgent], problem). problem is '' or why OpenClaw's agents could not be listed."""
    code, out, err = call([cli, "agents", "list", "--json"], env, run=run)
    if code != 0:
        return [], f"`openclaw agents list --json` failed: {(err or out).strip()[-300:]}"
    agents = parse_openclaw_agents(out)
    if not agents:
        return [], "OpenClaw lists no agents"
    port_text = openclaw_config_get(cli, "gateway.port", env, run)
    port = int(port_text) if port_text.isdigit() else DEFAULT_GATEWAY_PORT
    live, why = (health or gateway_health)(port)
    version = first_line(call([cli, "--version"], env, timeout=30, run=run)[1])
    version = version.split(" — ")[0].strip() if version else "OpenClaw"
    default_ws = ""
    if any(not a["workspace"] for a in agents):
        default_ws = openclaw_config_get(cli, "agents.defaults.workspace", env, run)
    found = []
    for a in agents:
        found.append(LocalAgent(
            kind=OPENCLAW, ref=a["id"], name=a["name"], available=live,
            detail=f"{version} agent '{a['id']}'" + (f", model {a['model']}" if a["model"] else "")
                   + f"; {why}",
            cli=cli, workspace=hostenv.expand(a["workspace"] or default_ws, env),
            is_default=a["is_default"]))
    return found, ""


# ---------------------------------------------------------------------------
# Hermes Agent
# ---------------------------------------------------------------------------

_BOX = re.compile(r"^[\s─-╿\-=]+$")


def parse_hermes_profiles(text: str) -> list:
    """`hermes profile list` (a table) -> [{"name", "model", "gateway", "sticky"}]. The sticky
    default is marked with a diamond. Header and rule lines are skipped."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or _BOX.match(line):
            continue
        sticky = "◆" in line
        cols = re.split(r"\s{2,}", line.replace("◆", " ").strip())
        if not cols or cols[0].lower() == "profile":
            continue
        name = cols[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
            continue
        out.append({"name": name, "model": cols[1].strip() if len(cols) > 1 else "",
                    "gateway": cols[2].strip() if len(cols) > 2 else "", "sticky": sticky})
    return out


def hermes_home(env: Mapping[str, str]) -> str:
    return hostenv.expand(env.get("HERMES_HOME") or "~/.hermes", env)


def profile_home(base: str, profile: str) -> str:
    return base if profile == "default" else os.path.join(base, "profiles", profile)


def discover_hermes(cli: str, env: Mapping[str, str], run: Optional[Runner] = None) -> tuple:
    """([LocalAgent], problem): the default profile and the named profiles."""
    base = hermes_home(env)
    code, out, err = call([cli, "profile", "list"], env, run=run)
    profiles = parse_hermes_profiles(out) if code == 0 else []
    if not profiles:
        # fall back to the folders: the default home and <home>/profiles/*
        names = ["default"] if os.path.isfile(os.path.join(base, "config.yaml")) else []
        pdir = os.path.join(base, "profiles")
        if os.path.isdir(pdir):
            names += sorted(n for n in os.listdir(pdir)
                            if os.path.isfile(os.path.join(pdir, n, "config.yaml")))
        profiles = [{"name": n, "model": "", "gateway": "", "sticky": n == "default"}
                    for n in names]
    if not profiles:
        return [], (f"`hermes profile list` failed: {(err or out).strip()[-300:]}" if code
                    else "Hermes has no profiles")
    found = []
    for p in profiles:
        home = profile_home(base, p["name"])
        ok = os.path.isdir(home)
        name = "Hermes" if p["name"] == "default" else f"Hermes {p['name']}"
        detail = f"Hermes Agent profile '{p['name']}'" + (f", model {p['model']}" if p["model"]
                                                          and p["model"] != "—" else "")
        if not ok:
            detail += f"; its folder {home} is missing"
        found.append(LocalAgent(kind=HERMES, ref=p["name"], name=name, available=ok,
                                detail=detail, cli=cli, home=home,
                                is_default=p["name"] == "default"))
    return found, ""


# ---------------------------------------------------------------------------
# Both
# ---------------------------------------------------------------------------

def is_shared(agent: LocalAgent, share: Optional[Iterable[str]]) -> bool:
    """`share_agents` entries match "kind:ref", the ref or the name (case-insensitive);
    None (missing) shares everything."""
    if share is None:
        return True
    wanted = {str(s).strip().casefold() for s in share}
    return bool({agent.key.casefold(), agent.ref.casefold(), agent.name.casefold()} & wanted)


def discover(cfg: Mapping[str, Any], path: str, environ: Optional[Mapping[str, str]] = None,
             run: Optional[Runner] = None, health: Optional[Callable[[int], tuple]] = None
             ) -> tuple:
    """All shared local agents: ([LocalAgent], [notes]). `cfg` is the agent-host config
    (`openclaw.command`, `hermes.command`, `share_agents`, `kinds`)."""
    env = hostenv.cli_env(path, environ)
    notes, agents = [], []
    kinds = cfg.get("kinds") or list(KINDS)
    for kind, discover_fn in ((OPENCLAW, discover_openclaw), (HERMES, discover_hermes)):
        if kind not in kinds:
            continue
        cli = hostenv.find_cli(kind, path, (cfg.get(kind) or {}).get("command", ""))
        if not cli:
            notes.append(f"{'OpenClaw' if kind == OPENCLAW else 'Hermes Agent'}: no `{kind}` "
                         "command found")
            continue
        if kind == OPENCLAW:
            found, problem = discover_openclaw(cli, env, run=run, health=health)
        else:
            found, problem = discover_fn(cli, env, run=run)
        if problem:
            notes.append(problem)
        agents.extend(a for a in found if is_shared(a, cfg.get("share_agents")))
        skipped = [a.key for a in found if not is_shared(a, cfg.get("share_agents"))]
        if skipped:
            notes.append("not shared (share_agents): " + ", ".join(skipped))
    return agents, notes
