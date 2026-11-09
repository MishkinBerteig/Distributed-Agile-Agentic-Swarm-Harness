"""CLI entry: python3 -m daash_runner --coordinator URL --token T [--config F] [--work-dir D] [--max-jobs N]."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys

from .client import AuthError, Client, CoordinatorError
from .config import load_config
from .runner import Runner


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


def setup_logging(work_dir: str) -> None:
    os.makedirs(work_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger("daash_runner")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(os.path.join(work_dir, "runner.log"), maxBytes=2_000_000, backupCount=3)
    sh = logging.StreamHandler(sys.stderr)
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("daash_runner needs Python 3.10 or newer", file=sys.stderr)
        return 2
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

    def _stop(signum, _frame):
        log.info("signal %s received", signum)
        runner.stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
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


if __name__ == "__main__":
    sys.exit(main())
