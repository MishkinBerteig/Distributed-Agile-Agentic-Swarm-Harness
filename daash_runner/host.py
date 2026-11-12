"""Agent hosts (docs/DESIGN-0.4.md 5.9): this machine's existing agents work for DAASH through a
runner that dials out. Nothing listens on the machine and no admin rights are needed.

    python3 ~/daash-runner.pyz connect <DAASH agent URL> <code> [--no-tools] [--no-service] [--name N]
    python3 ~/daash-runner.pyz status
    python3 ~/daash-runner.pyz run           (what the service runs)
    python3 ~/daash-runner.pyz disconnect

`connect` enrolls with the one-time code, writes ~/.daash-runner/config.json (0600), finds the
local agents, says hello (DAASH answers with an MCP token for each new agent), gives the agents
DAASH's tools (unless --no-tools), installs and starts a user service (unless --no-service) and
prints what it found and changed. `disconnect` undoes all of it and keeps the logs.

The config remembers every change `connect` (or a later hello) made, so `disconnect` removes
exactly those:

    {"version": 1, "coordinator", "token", "runner_id", "name", "host", "max_jobs",
     "tools": true, "share_agents": null | ["main", "hermes:default", "Fitz"], "path",
     "openclaw": {"command"}, "hermes": {"command"}, "harnesses": {},
     "installed": {"openclaw:main": {"kind", "ref", "cli", "url", "home"?}},
     "pending_tokens": {"openclaw:main": "dca_..."}, "tools_skipped": {"key": "why"},
     "service": {"kind", "path"} | null, "entry": [...], "connected_at"}
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import threading
import time
from typing import Any, Callable, Mapping, Optional

from . import agenttools, discovery, hostenv, service
from .client import AuthError, Client, CoordinatorError, HttpStatus
from .discovery import HERMES, OPENCLAW, LocalAgent

CONFIG_VERSION = 1
DEFAULT_MAX_JOBS = 2

Printer = Callable[[str], None]


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------

def load_host_config(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"{path} is not a JSON object")
    return cfg


def save_host_config(path: str, cfg: Mapping[str, Any]) -> None:
    """Atomic write, owner-only (0600): it holds the runner token."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def local_ref_parts(local_ref: str) -> tuple:
    """DAASH's `local_ref` is the agent's local id, or "<kind>:<id>" when two kinds share an id."""
    kind, sep, rest = (local_ref or "").partition(":")
    if sep and kind in discovery.KINDS and rest:
        return kind, rest
    return None, local_ref or ""


# ---------------------------------------------------------------------------
# The agent host
# ---------------------------------------------------------------------------

class AgentHost:
    """This machine's shared agents, their DAASH tools and how to run them. Used by `connect`
    and by the runner loop (`report` before every hello, `handle_hello` after it, `resolve`
    for each relay job)."""

    def __init__(self, cfg: dict, cfg_path: str, environ: Optional[Mapping[str, str]] = None,
                 run: Optional[Callable] = None, health: Optional[Callable] = None,
                 login_path: Optional[str] = None) -> None:
        self.cfg = cfg
        self.cfg_path = cfg_path
        own = dict(os.environ if environ is None else environ)
        # The owner's CLIs run in their login shell's environment (a service has almost none),
        # overlaid with this process's own. `login_path` given (tests, disconnect) = skip the shell.
        login = hostenv.login_shell_env(own) if login_path is None else {}
        self.environ = {**login, **own}
        self.run = run
        self.health = health
        self.path = hostenv.search_path(own, cfg.get("path", ""),
                                        login.get("PATH", "") if login_path is None else login_path)
        self.agents: dict = {}
        self.notes: list = []
        self._lock = threading.RLock()
        for key in ("installed", "pending_tokens", "tools_skipped"):
            cfg.setdefault(key, {})

    # -- environment ---------------------------------------------------------------------

    def env(self) -> dict:
        return hostenv.cli_env(self.path, self.environ)

    def save(self) -> None:
        with self._lock:
            save_host_config(self.cfg_path, self.cfg)

    @property
    def mcp_url(self) -> str:
        return agenttools.mcp_url(self.cfg["coordinator"])

    # -- discovery ------------------------------------------------------------------------

    def discover(self) -> list:
        found, notes = discovery.discover(self.cfg, self.path, self.environ, run=self.run,
                                          health=self.health)
        with self._lock:
            self.agents = {a.key: a for a in found}
            self.notes = notes
        return found

    def tools_agent_keys(self) -> set:
        """Agents that can have DAASH tools: every Hermes profile; for OpenClaw, whose MCP servers
        are shared by all its agents, one agent per install (the default one, else the first)."""
        with self._lock:
            agents = list(self.agents.values())
        keys = {a.key for a in agents if a.kind == HERMES}
        claws = sorted((a for a in agents if a.kind == OPENCLAW),
                       key=lambda a: (not a.is_default, a.ref))
        if claws:
            keys.add(claws[0].key)
        return keys

    def has_tools(self, agent: LocalAgent) -> bool:
        return agent.key in self.cfg["installed"]

    def report(self) -> list:
        """The hello's `agents`: rediscover, then describe each shared agent."""
        self.discover()
        can = self.tools_agent_keys()
        out = []
        with self._lock:
            for a in self.agents.values():
                want = (bool(self.cfg.get("tools", True)) and a.key in can
                        and a.key not in self.cfg["installed"]
                        and a.key not in self.cfg["pending_tokens"]
                        and a.key not in self.cfg["tools_skipped"])
                out.append(a.report(tools=a.key in self.cfg["installed"], new_token=want))
        return out

    def handle_hello(self, info: Mapping[str, Any]) -> list:
        """Keep the new MCP tokens DAASH sent and give those agents DAASH tools (and retry the
        tokens still pending from an earlier hello). Returns lines describing what changed."""
        tokens = info.get("agent_tokens") or {}
        listed = info.get("agents") or []
        if not tokens:
            return self.install_pending()
        can = self.tools_agent_keys()
        by_ref = {}
        for a in listed:
            if isinstance(a, dict) and a.get("local_ref"):
                kind = a.get("kind") or local_ref_parts(a["local_ref"])[0]
                ref = a.get("ref") or local_ref_parts(a["local_ref"])[1]
                by_ref[a["local_ref"]] = f"{kind}:{ref}"
        with self._lock:
            for local_ref, token in tokens.items():
                key = by_ref.get(local_ref)
                if key is None:
                    kind, ref = local_ref_parts(local_ref)
                    matches = [k for k in self.agents if k.endswith(":" + ref)
                               and (kind is None or k.startswith(kind + ":"))]
                    key = matches[0] if len(matches) == 1 else None
                if key is None or not self.cfg.get("tools", True) or key not in can:
                    continue
                self.cfg["pending_tokens"][key] = token
                self.cfg["tools_skipped"].pop(key, None)
            self.save()
        return self.install_pending()

    def install_pending(self) -> list:
        """Give DAASH tools to the agents with a pending token. A token stays pending (in the
        0600 config) when its agent is not found right now; it is dropped when the agent's CLI
        refuses for good (a different `daash` MCP server exists), with the reason kept; after a
        failure that may be temporary it stays pending and the next hello tries again."""
        lines = []
        with self._lock:
            pending = dict(self.cfg["pending_tokens"])
            agents = dict(self.agents)
        env = self.env()
        for key, token in pending.items():
            agent = agents.get(key)
            if agent is None:
                continue
            if agent.kind == OPENCLAW:
                ok, msg, retry = agenttools.install_openclaw(agent.cli, env, self.mcp_url, token,
                                                             self.run)
                record = {"kind": OPENCLAW, "ref": agent.ref, "cli": agent.cli, "url": self.mcp_url}
            else:
                ok, msg, retry = agenttools.install_hermes(agent.cli, env, agent.home, self.mcp_url,
                                                           token, self.run)
                record = {"kind": HERMES, "ref": agent.ref, "cli": agent.cli, "url": self.mcp_url,
                          "home": agent.home}
            with self._lock:
                if ok:
                    self.cfg["pending_tokens"].pop(key, None)
                    self.cfg["installed"][key] = record
                elif not retry:
                    self.cfg["pending_tokens"].pop(key, None)
                    self.cfg["tools_skipped"][key] = msg
                self.save()
            lines.append(f"{agent.name}: {msg}" + (" (will try again)" if not ok and retry else ""))
        return lines

    def remove_tools(self) -> list:
        """Undo every DAASH tool install recorded in the config."""
        lines = []
        env = self.env()
        for key, rec in list(self.cfg["installed"].items()):
            cli = rec.get("cli") or hostenv.find_cli(rec.get("kind", ""), self.path) or ""
            if not cli or not os.path.exists(cli):
                lines.append(f"{key}: its CLI ({cli or rec.get('kind')}) is gone; nothing to undo")
                self.cfg["installed"].pop(key, None)
                continue
            if rec.get("kind") == OPENCLAW:
                ok, msg = agenttools.remove_openclaw(cli, env, rec.get("url") or self.mcp_url,
                                                     self.run)
            else:
                ok, msg = agenttools.remove_hermes(cli, env, rec.get("home", ""),
                                                   rec.get("url") or self.mcp_url, self.run)
            lines.append(f"{key}: {msg}")
            if ok:
                self.cfg["installed"].pop(key, None)
        self.cfg["pending_tokens"] = {}
        return lines

    def resolve(self, relay_name: str, local_ref: str) -> Optional[LocalAgent]:
        """The local agent a relay job names; rediscovers once when it is not known yet."""
        kind = discovery.KIND_OF_RELAY.get(relay_name)
        pkind, ref = local_ref_parts(local_ref)
        if kind is None or (pkind and pkind != kind):
            return None
        key = f"{kind}:{ref}"
        with self._lock:
            agent = self.agents.get(key)
        if agent is None:
            self.discover()
            with self._lock:
                agent = self.agents.get(key)
        return agent


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _host_name(environ: Mapping[str, str]) -> str:
    return (environ.get("DAASH_RUNNER_HOST") or platform.node() or "agent-host").split(".")[0]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def default_entry() -> list:
    """How a service starts this runner: the zipapp it was started from, or the package."""
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if argv0.endswith(".pyz") and os.path.isfile(argv0):
        return [argv0]
    return ["-m", "daash_runner"]


def cmd_connect(url: str, code: str, *, no_tools: bool = False, no_service: bool = False,
                name: str = "", environ: Optional[Mapping[str, str]] = None,
                run: Optional[Callable] = None, health: Optional[Callable] = None,
                login_path: Optional[str] = None, entry: Optional[list] = None,
                out: Printer = print, version: str = "", client_factory=Client) -> int:
    env = dict(os.environ if environ is None else environ)
    cfg_path = hostenv.config_path(env)
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        out(f"error: '{url}' is not a DAASH address (expected http://host:8001 or https://...)")
        return 2
    try:
        existing = load_host_config(cfg_path)
    except (OSError, ValueError) as exc:
        out(f"error: cannot read {cfg_path}: {exc}")
        return 2
    if existing and existing.get("token"):
        out(f"This machine is already connected to DAASH at {existing.get('coordinator')} as runner "
            f"'{existing.get('name')}'. To connect it again, first run:\n"
            f"  python3 ~/daash-runner.pyz disconnect")
        return 2
    host = _host_name(env)
    out(f"Connecting this machine ({host}) to DAASH at {url} ...")
    try:
        enrolled = client_factory(url, "").enroll(code.strip(), host, name or host, version,
                                                  f"{platform.system()} {platform.machine()}, "
                                                  f"Python {platform.python_version()}")
    except HttpStatus as exc:
        detail = exc.body
        try:
            detail = json.loads(exc.body).get("detail") or exc.body
        except (ValueError, AttributeError):
            pass
        out(f"error: DAASH refused the connect code: {detail}")
        return 2
    except (CoordinatorError, AuthError) as exc:
        out(f"error: cannot reach DAASH at {url}: {exc}\n"
            "Check the address (it is DAASH's agent port, usually :8001) and that this machine can "
            "reach it (e.g. Tailscale is up).")
        return 2
    path_env = hostenv.search_path(env, "", login_path)
    cfg = {"version": CONFIG_VERSION, "coordinator": url, "token": enrolled["token"],
           "runner_id": enrolled.get("runner_id"), "name": enrolled.get("name") or host,
           "host": host, "max_jobs": DEFAULT_MAX_JOBS, "tools": not no_tools,
           "share_agents": None, "path": path_env, "openclaw": {}, "hermes": {},
           "harnesses": {}, "installed": {}, "pending_tokens": {}, "tools_skipped": {},
           "service": None, "entry": entry or default_entry(), "connected_at": _now_iso()}
    save_host_config(cfg_path, cfg)
    changed = [f"wrote {cfg_path} (mode 0600; holds this machine's DAASH runner token)"]
    out(f"Enrolled as runner '{cfg['name']}'.")

    agent_host = AgentHost(cfg, cfg_path, env, run=run, health=health, login_path=path_env)
    report = agent_host.report()
    for k in ("openclaw", "hermes"):
        found_cli = hostenv.find_cli(k, agent_host.path)
        if found_cli:
            cfg[k] = {"command": found_cli}
    agent_host.save()
    out("")
    out(f"Agents found on this machine: {len(report)}")
    for a in agent_host.agents.values():
        out(f"  - {a.name} ({a.kind} '{a.ref}'): {'available' if a.available else 'NOT available'}"
            f" - {a.detail}")
    for note in agent_host.notes:
        out(f"  note: {note}")

    from .runner import VERSION
    try:
        info = client_factory(url, cfg["token"]).hello(host, VERSION, cfg["max_jobs"], [], agents=report)
    except (CoordinatorError, AuthError, HttpStatus) as exc:
        out(f"warning: the first hello failed ({exc}); the runner will retry when it runs.")
        info = {}
    tool_lines = agent_host.handle_hello(info) if info else []
    if info.get("agents"):
        out("")
        out("In DAASH they appear as:")
        for a in info["agents"]:
            out(f"  - {a.get('name')} ({a.get('status')})")
    if no_tools:
        out("\nDAASH tools: not added (--no-tools).")
    elif tool_lines:
        out("")
        out("DAASH tools (MCP server 'daash'):")
        for line in tool_lines:
            out(f"  - {line}")
    claws = [a for a in agent_host.agents.values() if a.kind == OPENCLAW]
    if not no_tools and len(claws) > 1:
        keys = agent_host.tools_agent_keys()
        others = [a.name for a in claws if a.key not in keys]
        out(f"  note: OpenClaw shares MCP servers between all its agents, so only one OpenClaw agent "
            f"gets DAASH tools; {', '.join(others)} work without them.")
    changed += [f"{k}: DAASH tools added" for k in cfg["installed"]]

    if no_service:
        out("\nService: not installed (--no-service). Start the runner yourself:\n"
            "  python3 ~/daash-runner.pyz run")
    else:
        kind = service.service_kind()
        argv = service.run_command(hostenv.python_for_service(env), cfg["entry"])
        ok, spath, msgs = service.install(kind, argv, path_env, env, run=run) if kind else \
            (False, "", ["no user service manager on this platform; run "
                         "`python3 ~/daash-runner.pyz run` yourself"])
        if spath:
            cfg["service"] = {"kind": kind, "path": spath, "argv": argv}
            agent_host.save()
        out("")
        for m in msgs:
            out(("Service: " if ok else "warning: ") + m)
        if ok:
            changed.append(f"{spath}: user service that runs the DAASH runner")
    out("")
    out("Changed on this machine:")
    for c in changed:
        out(f"  - {c}")
    out(f"Logs: {hostenv.logs_dir(env)}")
    out("Undo everything: python3 ~/daash-runner.pyz disconnect")
    return 0


def cmd_disconnect(*, environ: Optional[Mapping[str, str]] = None, run: Optional[Callable] = None,
                   out: Printer = print, client_factory=Client, login_path: Optional[str] = None
                   ) -> int:
    env = dict(os.environ if environ is None else environ)
    cfg_path = hostenv.config_path(env)
    try:
        cfg = load_host_config(cfg_path)
    except (OSError, ValueError) as exc:
        out(f"error: cannot read {cfg_path}: {exc}")
        return 2
    kind = service.service_kind()
    if cfg is None:
        out(f"This machine is not connected to DAASH ({cfg_path} does not exist).")
        if kind and os.path.exists(service.service_file(kind, env)):
            ok, msgs = service.uninstall(kind, "", env, run=run)
            for m in msgs:
                out(f"  - {m}")
        return 0
    out(f"Disconnecting this machine from DAASH at {cfg.get('coordinator')} ...")
    done = []
    svc = cfg.get("service") or {}
    if svc.get("kind") or (kind and os.path.exists(service.service_file(kind, env))):
        _, msgs = service.uninstall(svc.get("kind") or kind, svc.get("path", ""), env, run=run)
        done += msgs
    agent_host = AgentHost(cfg, cfg_path, env, run=run, login_path=login_path or cfg.get("path", ""))
    done += agent_host.remove_tools()
    leftover = dict(cfg.get("installed") or {})
    if cfg.get("token"):
        try:
            client_factory(cfg["coordinator"], cfg["token"]).disconnect()
            done.append("told DAASH: this runner is disabled and its agents are offline")
        except (CoordinatorError, AuthError, HttpStatus) as exc:
            done.append(f"could not tell DAASH ({exc}); disable the runner in DAASHboard")
    state = hostenv.state_dir(env)
    for sub in ("jobs", "state"):
        p = os.path.join(state, sub)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            done.append(f"removed {p}")
    if leftover:
        cfg["token"] = ""
        save_host_config(cfg_path, cfg)
        done.append(f"kept {cfg_path} without its token: some changes could not be undone "
                    f"({', '.join(leftover)}); run disconnect again after fixing them")
    else:
        os.remove(cfg_path)
        done.append(f"removed {cfg_path}")
    for line in done:
        out(f"  - {line}")
    out(f"Kept: the logs in {hostenv.logs_dir(env)} and ~/daash-runner.pyz (delete them if you like).")
    return 0 if not leftover else 1


def cmd_status(*, environ: Optional[Mapping[str, str]] = None, run: Optional[Callable] = None,
               out: Printer = print, client_factory=Client, health: Optional[Callable] = None,
               login_path: Optional[str] = None) -> int:
    env = dict(os.environ if environ is None else environ)
    cfg_path = hostenv.config_path(env)
    try:
        cfg = load_host_config(cfg_path)
    except (OSError, ValueError) as exc:
        out(f"error: cannot read {cfg_path}: {exc}")
        return 2
    if not cfg or not cfg.get("token"):
        out(f"Not connected to DAASH ({cfg_path} has no runner token).")
        return 1
    out(f"DAASH:    {cfg['coordinator']} (runner '{cfg.get('name')}', host {cfg.get('host')})")
    try:
        client_factory(cfg["coordinator"], "").health()
        out("          reachable")
    except (CoordinatorError, AuthError, HttpStatus) as exc:
        out(f"          NOT reachable: {exc}")
    kind = (cfg.get("service") or {}).get("kind") or service.service_kind()
    out(f"Service:  {service.status(kind, env, run=run) if kind else 'none'}")
    out(f"Tools:    {'added to new agents' if cfg.get('tools', True) else 'off (--no-tools)'}")
    agent_host = AgentHost(cfg, cfg_path, env, run=run, health=health,
                           login_path=login_path if login_path is not None else cfg.get("path", ""))
    agent_host.discover()
    out(f"Agents:   {len(agent_host.agents)} shared")
    for a in agent_host.agents.values():
        tools = "DAASH tools" if a.key in cfg["installed"] else (
            "no DAASH tools" + (f" ({cfg['tools_skipped'][a.key]})" if a.key in cfg["tools_skipped"]
                                else ""))
        out(f"  - {a.name} ({a.kind} '{a.ref}'): {'available' if a.available else 'NOT available'}"
            f"; {tools}; {a.detail}")
    for note in agent_host.notes:
        out(f"  note: {note}")
    out(f"Logs:     {hostenv.logs_dir(env)}")
    return 0
