"""CLI entry.

Runner (docs/DESIGN-0.3.md section 7), flags or environment:
    python3 -m daash_runner --coordinator URL --token T [--config F] [--work-dir D] [--max-jobs N]

Agent host (docs/DESIGN-0.4.md 5.9), usually from the zipapp DAASH serves:
    python3 ~/daash-runner.pyz connect <DAASH agent URL> <code> [--no-tools] [--no-service] [--name N]
    python3 ~/daash-runner.pyz status | run | disconnect

Standard library only; Python 3.9 or newer.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys

from .client import AuthError, Client, CoordinatorError
from .config import config_from, load_config
from .runner import VERSION, Runner

SUBCOMMANDS = ("connect", "disconnect", "status", "run")


def parse_args(argv: list[str] | None, environ=None) -> argparse.Namespace:
    env = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(prog="python3 -m daash_runner", description="DAASH pull-based runner")
    ap.add_argument("--coordinator", default=env.get("DAASH_COORDINATOR_URL", ""), help="coordinator base URL")
    ap.add_argument("--token", default=env.get("DAASH_RUNNER_TOKEN", ""), help="runner bearer token (daash_rt_...)")
    ap.add_argument("--config", default=env.get("DAASH_RUNNER_CONFIG", ""), help="runner.json")
    ap.add_argument("--work-dir", default=env.get("DAASH_RUNNER_WORKDIR", "~/.daash-runner"))
    ap.add_argument("--max-jobs", type=int, default=int(env.get("DAASH_RUNNER_MAX_JOBS", "1") or 1))
    ap.add_argument("--host", default=env.get("DAASH_RUNNER_HOST", ""), help="host name to report")
    return ap.parse_args(argv)


def host_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python3 ~/daash-runner.pyz",
        description="DAASH agent host: let this machine's agents (OpenClaw, Hermes Agent) work for "
                    "DAASH. The runner dials out to DAASH; nothing listens on this machine.")
    sub = ap.add_subparsers(dest="command", metavar="{connect,status,run,disconnect}")
    c = sub.add_parser("connect", help="enroll with a one-time code, give the local agents DAASH "
                                       "tools and start the runner as a user service")
    c.add_argument("url", help="DAASH's agent address, e.g. http://daash-host:8001")
    c.add_argument("code", help="the one-time connect code from DAASHboard")
    c.add_argument("--no-tools", action="store_true",
                   help="do not add DAASH's MCP server to the agents' configs")
    c.add_argument("--no-service", action="store_true",
                   help="do not install a launchd/systemd user service (run it yourself)")
    c.add_argument("--name", default="", help="runner name shown in DAASH (default: host name)")
    sub.add_parser("status", help="show the connection, the service and the local agents")
    sub.add_parser("run", help="run the runner in the foreground (what the service does)")
    sub.add_parser("disconnect", help="undo connect: stop the service, remove DAASH tools, "
                                      "forget the token (logs are kept)")
    return ap


def setup_logging(work_dir: str, log_dir: str = "") -> None:
    os.makedirs(log_dir or work_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger("daash_runner")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir or work_dir, "runner.log"),
                                              maxBytes=2_000_000, backupCount=3)
    sh = logging.StreamHandler(sys.stderr)
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)


def _install_signals(runner: Runner, log: logging.Logger) -> None:
    def _stop(signum, _frame):
        log.info("signal %s received", signum)
        runner.stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)


def run_host(environ=None) -> int:
    """`run` on an agent host: the runner from ~/.daash-runner/config.json, relaying to the
    local agents (and running any harnesses the config lists)."""
    from . import hostenv
    from .host import AgentHost, load_host_config

    env = dict(os.environ if environ is None else environ)
    cfg_path = hostenv.config_path(env)
    try:
        cfg = load_host_config(cfg_path)
    except (OSError, ValueError) as exc:
        print(f"cannot read {cfg_path}: {exc}", file=sys.stderr)
        return 2
    if not cfg or not cfg.get("token"):
        print(f"This machine is not connected to DAASH ({cfg_path} has no runner token). Run "
              "`python3 ~/daash-runner.pyz connect <url> <code>` first.", file=sys.stderr)
        return 2
    work_dir = hostenv.state_dir(env)
    setup_logging(work_dir, hostenv.logs_dir(env))
    log = logging.getLogger("daash_runner")
    # an agent host relays to its agents; it runs harness CLIs only if the owner lists them
    rcfg = config_from({"harnesses": cfg.get("harnesses") or {}}, env)
    agent_host = AgentHost(cfg, cfg_path, env)
    runner = Runner(Client(cfg["coordinator"], cfg["token"]), rcfg, work_dir,
                    int(cfg.get("max_jobs") or 2), host=cfg.get("host") or None, environ=env,
                    agent_host=agent_host)
    _install_signals(runner, log)
    log.info("agent host '%s' connecting to %s", cfg.get("name"), cfg["coordinator"])
    runner.run()     # retries 401s and outages with backoff; a disabled runner just waits
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 9):
        print("daash_runner needs Python 3.9 or newer", file=sys.stderr)
        return 2
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        return host_main(argv)
    if argv[:1] in (["-h"], ["--help"]):
        host_parser().print_help()
        print("\nA classic runner (docs/RUNNER.md) instead takes: --coordinator URL --token T "
              "[--config runner.json] [--work-dir D] [--max-jobs N] [--host NAME]")
        return 0
    args = parse_args(argv)
    if not args.coordinator or not args.token:
        print("--coordinator and --token (or DAASH_COORDINATOR_URL / DAASH_RUNNER_TOKEN) are required",
              file=sys.stderr)
        return 2
    work_dir = os.path.abspath(os.path.expanduser(args.work_dir))
    setup_logging(work_dir)
    log = logging.getLogger("daash_runner")
    try:
        cfg = load_config(args.config or None)
    except (OSError, ValueError) as exc:
        log.error("cannot load config %s: %s", args.config, exc)
        return 2
    runner = Runner(Client(args.coordinator, args.token), cfg, work_dir, args.max_jobs, host=args.host or None)
    _install_signals(runner, log)
    # A wrong token at startup is a configuration error: fail loudly instead of retrying forever.
    try:
        runner.hello()
    except AuthError:
        log.error("the coordinator rejected the runner token (401). Check --token / DAASH_RUNNER_TOKEN.")
        return 2
    except CoordinatorError as exc:
        log.warning("coordinator not reachable yet (%s); will keep trying", exc)
    runner.run()
    return 0


def host_main(argv: list[str]) -> int:
    from . import host

    args = host_parser().parse_args(argv)
    if args.command == "connect":
        return host.cmd_connect(args.url, args.code, no_tools=args.no_tools,
                                no_service=args.no_service, name=args.name, version=VERSION)
    if args.command == "disconnect":
        return host.cmd_disconnect()
    if args.command == "status":
        return host.cmd_status()
    if args.command == "run":
        if os.environ.get("DAASH_COORDINATOR_URL") and os.environ.get("DAASH_RUNNER_TOKEN"):
            return main(argv[1:])      # a classic runner configured by its environment
        return run_host()
    host_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
