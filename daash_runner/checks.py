"""Command checks (docs/DESIGN-0.3.md section 12): run the coordinator's acceptance-check
commands in a job workspace after the agent CLI finished, and report exit code + output tail."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from typing import Callable, Optional

OUTPUT_TAIL_MAX = 4000
DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 600.0


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def run_command(command: str, cwd: str, env: Optional[dict], timeout: float,
                tick: Optional[Callable[[], bool]] = None, poll: float = 0.2) -> Optional[dict]:
    """Run one shell command; returns {exit_code, output_tail, duration_ms, timed_out}, or None
    when `tick()` returned True (cancelled/shutdown) and the command was killed."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(  # noqa: S602 - the command is the coordinator's check
                command, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return {"exit_code": None, "output_tail": f"could not start the command: {exc}",
                    "duration_ms": 0, "timed_out": False}
        timed_out = False
        deadline = started + timeout
        while proc.poll() is None:
            if tick is not None and tick():
                _kill_group(proc)
                proc.wait()
                return None
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_group(proc)
                proc.wait()
                break
            time.sleep(poll)
        out.seek(0, os.SEEK_END)
        size = out.tell()
        out.seek(max(0, size - OUTPUT_TAIL_MAX * 4))
        tail = out.read().decode("utf-8", errors="replace")[-OUTPUT_TAIL_MAX:]
    return {"exit_code": None if timed_out else proc.returncode, "output_tail": tail,
            "duration_ms": int((time.monotonic() - started) * 1000), "timed_out": timed_out}


def run_checks(checks: list, cwd: str, env: Optional[dict] = None,
               tick: Optional[Callable[[], bool]] = None, poll: float = 0.2) -> Optional[list[dict]]:
    """Run each `{"id", "command", "timeout_seconds"}`; the list of `check_results`, or None when
    aborted by `tick`. Malformed entries are skipped."""
    results: list[dict] = []
    for chk in checks or []:
        if not isinstance(chk, dict) or not isinstance(chk.get("id"), str) \
                or not isinstance(chk.get("command"), str) or not chk["command"].strip():
            continue
        try:
            timeout = float(chk.get("timeout_seconds") or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(0.2, min(timeout, MAX_TIMEOUT))
        r = run_command(chk["command"], cwd, env, timeout, tick, poll)
        if r is None:
            return None
        results.append({"id": chk["id"], **r})
    return results
