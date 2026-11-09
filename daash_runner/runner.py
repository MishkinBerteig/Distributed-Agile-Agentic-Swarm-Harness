"""The runner loop: hello, lease, run jobs in worker threads, heartbeat, upload results."""
from __future__ import annotations

import logging
import os
import platform
import random
import shutil
import signal
import subprocess
import threading
import time
from typing import Any

from . import adapters, workspace
from .client import AuthError, Client, CoordinatorError, HttpStatus
from .checks import run_checks
from .config import available_harnesses

VERSION = "0.3.0"
log = logging.getLogger("daash_runner")

SUMMARY_MAX = 4000
TAIL_MAX = 20000
PRUNE_AFTER_SECONDS = 3 * 24 * 3600
REHELLO_SECONDS = 300


def prune_jobs(jobs_dir: str, max_age_seconds: float = PRUNE_AFTER_SECONDS, now: float | None = None) -> int:
    """Delete job folders whose last modification is older than max_age_seconds."""
    if not os.path.isdir(jobs_dir):
        return 0
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    for name in os.listdir(jobs_dir):
        path = os.path.join(jobs_dir, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


def _tail(path: str, limit: int) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _kill_group(proc: subprocess.Popen, grace: float = 3.0) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


class Runner:
    def __init__(self, client: Client, cfg: dict, work_dir: str, max_jobs: int = 1,
                 host: str | None = None, environ: dict | None = None) -> None:
        self.client = client
        self.cfg = cfg
        self.work_dir = os.path.abspath(os.path.expanduser(work_dir))
        self.jobs_dir = os.path.join(self.work_dir, "jobs")
        self.state_dir = os.path.join(self.work_dir, "state")
        self.max_jobs = max(1, int(max_jobs))
        self.host = host or platform.node() or "unknown"
        self.environ = environ
        self.stop = threading.Event()
        self.heartbeat_seconds = 15.0
        self.lease_wait = 20
        self.rehello_seconds = REHELLO_SECONDS
        self.initial_backoff = 1.0
        self.max_backoff = 60.0
        self.poll_interval = 0.1      # supervise loop tick (tests shorten it)
        self.idle_wait = 1.0          # pause when nothing to lease (tests shorten it)
        self.upload_attempts = 6
        self._lock = threading.Lock()
        self._active: dict[str, threading.Thread] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self.name = ""
        os.makedirs(self.jobs_dir, exist_ok=True)
        os.makedirs(self.state_dir, exist_ok=True)

    # -- protocol -------------------------------------------------------------

    def hello(self) -> dict:
        info = self.client.hello(self.host, VERSION, self.max_jobs, available_harnesses(self.cfg))
        self.name = info.get("name", self.name)
        self.heartbeat_seconds = float(info.get("heartbeat_seconds") or self.heartbeat_seconds)
        return info

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def run(self) -> None:
        backoff = self.initial_backoff
        last_hello = 0.0
        prune_jobs(self.jobs_dir)
        while not self.stop.is_set():
            try:
                if time.monotonic() - last_hello >= self.rehello_seconds or last_hello == 0.0:
                    info = self.hello()
                    last_hello = time.monotonic()
                    log.info("hello ok: runner '%s', harnesses=%s", info.get("name"),
                             [h["harness"] for h in available_harnesses(self.cfg)])
                    prune_jobs(self.jobs_dir)
                free = self.max_jobs - self.active_count()
                if free <= 0:
                    self.stop.wait(self.idle_wait)
                    continue
                job = self.client.lease(self.lease_wait, free)
                backoff = self.initial_backoff
                if job:
                    self._start(job)
                elif self.lease_wait <= 0:
                    self.stop.wait(self.idle_wait)
            except AuthError as exc:
                log.error("coordinator rejected the runner token (401): %s", str(exc)[:200])
                backoff = self._sleep_backoff(backoff, minimum=10.0)
            except CoordinatorError as exc:
                log.warning("coordinator error: %s (retry in %.0fs)", exc, backoff)
                backoff = self._sleep_backoff(backoff)
            except HttpStatus as exc:
                log.warning("coordinator said %s", exc)
                backoff = self._sleep_backoff(backoff)
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("unexpected error in runner loop")
                backoff = self._sleep_backoff(backoff)
        self._shutdown()

    def _sleep_backoff(self, backoff: float, minimum: float = 0.0) -> float:
        delay = max(backoff, minimum)
        self.stop.wait(delay * (0.75 + random.random() * 0.5))
        return min(delay * 2, self.max_backoff)

    def _shutdown(self) -> None:
        log.info("shutting down: stopping %d running job(s)", self.active_count())
        with self._lock:
            threads = list(self._active.values())
        for t in threads:
            t.join(timeout=30)

    # -- jobs -----------------------------------------------------------------

    def _start(self, job: dict) -> None:
        t = threading.Thread(target=self._job_thread, args=(job,), name=f"job-{job['id']}")
        with self._lock:
            self._active[job["id"]] = t
        t.start()
        log.info("leased job %s (%s/%s, task %s)", job["id"], job.get("harness"), job.get("model"), job.get("task_id"))

    def _job_thread(self, job: dict) -> None:
        jid = job["id"]
        try:
            payload = self.execute(job)
            if payload is not None:
                self.upload(jid, payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s crashed", jid)
            try:
                self.upload(jid, self._error_payload(f"runner crashed: {exc}"))
            except Exception:  # noqa: BLE001
                log.exception("could not report crash of job %s", jid)
        finally:
            with self._lock:
                self._active.pop(jid, None)

    @staticmethod
    def _error_payload(reason: str, **extra: Any) -> dict:
        p = {"outcome": "error", "summary": "", "files": [], "deleted": [],
             "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
             "cost_usd": None, "duration_ms": 0, "exit_code": None, "transcript_tail": "", "error": reason[:2000]}
        p.update(extra)
        return p

    def execute(self, job: dict) -> dict | None:
        """Run one job; returns the result payload, or None when the lease was cancelled."""
        jid = job["id"]
        jdir = os.path.join(self.jobs_dir, jid)
        ws = os.path.join(jdir, "ws")
        os.makedirs(jdir, exist_ok=True)
        harness = job.get("harness", "")
        hcfg = self.cfg["harnesses"].get(harness)
        if hcfg is None or harness not in adapters.ADAPTERS:
            return self._error_payload(f"this runner has no harness '{harness}' configured")
        started = time.monotonic()
        tar_path = os.path.join(jdir, "workspace.tar.gz")
        try:
            self.client.download_workspace(jid, tar_path, job.get("workspace_url"))
            workspace.safe_extract(tar_path, ws)
        except workspace.UnsafeArchive as exc:
            return self._error_payload(f"unsafe workspace archive: {exc}")
        except HttpStatus as exc:
            if exc.status in (404, 409):
                log.info("job %s: workspace not available (%s), dropping", jid, exc.status)
                return None
            return self._error_payload(f"workspace download failed: {exc}")
        finally:
            try:
                os.remove(tar_path)
            except OSError:
                pass
        before = workspace.snapshot(ws)

        state = os.path.join(self.state_dir, harness)
        os.makedirs(state, exist_ok=True)
        task = job.get("task") or {}
        spec = adapters.JobSpec(model=job.get("model", ""), system_prompt=job.get("system_prompt", ""),
                                prompt=job.get("prompt", ""), state_dir=state, artifact=task.get("artifact") or "")
        inv = adapters.build(harness, hcfg, spec, self.environ)
        limits = job.get("limits") or {}
        timeout = float(min(limits.get("timeout_seconds") or hcfg["timeout_seconds"], hcfg["timeout_seconds"]))
        out_path, err_path = os.path.join(jdir, "stdout.log"), os.path.join(jdir, "stderr.log")

        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            try:
                proc = subprocess.Popen(  # noqa: S603 - argv is operator-configured harness CLI
                    inv.argv, cwd=ws, env=inv.env, stdin=subprocess.PIPE, stdout=out, stderr=err,
                    start_new_session=True)
            except FileNotFoundError:
                return self._error_payload(f"harness CLI '{inv.argv[0]}' not found on this runner")
            except OSError as exc:
                return self._error_payload(f"could not start '{inv.argv[0]}': {exc}")
            with self._lock:
                self._procs[jid] = proc
            threading.Thread(target=self._feed_stdin, args=(proc, inv.stdin), daemon=True).start()
            state_flag = self._supervise(jid, proc, timeout)

        with self._lock:
            self._procs.pop(jid, None)
        if state_flag == "cancelled":
            log.info("job %s cancelled by the coordinator; dropped", jid)
            return None

        stdout, stderr = _tail(out_path, 20_000_000), _tail(err_path, 200_000)
        exit_code = proc.returncode
        parsed = adapters.parse(harness, stdout, stderr, exit_code)
        if state_flag == "timeout":
            parsed.outcome, parsed.error = "failed", f"the agent timed out after {int(timeout)} seconds"
        elif state_flag == "shutdown":
            parsed.outcome, parsed.error = "error", "the runner was shut down while the job was running"

        files: list[dict] = []
        deleted: list[str] = []
        if parsed.outcome != "error":
            try:
                changed, deleted = workspace.diff(before, workspace.snapshot(ws))
                files = workspace.encode_files(ws, changed, limits)
            except workspace.LimitExceeded as exc:
                parsed.outcome, parsed.error, files, deleted = "failed", str(exc), [], []
        check_results: list[dict] = []
        if parsed.outcome == "submitted" and job.get("checks"):
            check_results = self._run_checks(jid, job["checks"], ws, inv.env)
            if check_results is None:
                log.info("job %s cancelled during its checks; dropped", jid)
                return None
        transcript = stdout[-TAIL_MAX:]
        if stderr.strip():
            transcript = (transcript[-(TAIL_MAX - 2100):] + "\n[stderr]\n" + stderr[-2000:])
        payload = {
            "outcome": parsed.outcome, "summary": parsed.final_text[:SUMMARY_MAX], "files": files,
            "deleted": deleted, "usage": parsed.usage, "cost_usd": parsed.cost_usd,
            "duration_ms": int((time.monotonic() - started) * 1000), "exit_code": exit_code,
            "transcript_tail": transcript[-TAIL_MAX:], "error": parsed.error[:2000],
        }
        if check_results:
            payload["check_results"] = check_results
        log.info("job %s finished: outcome=%s files=%d deleted=%d exit=%s", jid, parsed.outcome,
                 len(files), len(deleted), exit_code)
        return payload

    def _run_checks(self, jid: str, checks: list, ws: str, env: dict | None) -> list[dict] | None:
        """Run the job's command checks in its workspace, heartbeating meanwhile. None = the lease
        was cancelled (or the runner is shutting down) and the job should be dropped."""
        next_beat = time.monotonic() + self.heartbeat_seconds

        def tick() -> bool:
            nonlocal next_beat
            if self.stop.is_set():
                return True
            if time.monotonic() >= next_beat:
                next_beat = time.monotonic() + self.heartbeat_seconds
                try:
                    return self.client.heartbeat(jid) == "cancelled"
                except Exception as exc:  # noqa: BLE001 - a bad heartbeat must not kill the job
                    log.warning("heartbeat for %s failed: %s", jid, exc)
            return False

        return run_checks(checks, ws, env, tick, poll=self.poll_interval)

    @staticmethod
    def _feed_stdin(proc: subprocess.Popen, text: str | None) -> None:
        try:
            if proc.stdin:
                if text:
                    proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _supervise(self, jid: str, proc: subprocess.Popen, timeout: float) -> str:
        """Wait for the CLI while heartbeating. Returns done | timeout | cancelled | shutdown."""
        deadline = time.monotonic() + timeout
        next_beat = time.monotonic() + self.heartbeat_seconds
        while proc.poll() is None:
            now = time.monotonic()
            if self.stop.is_set():
                _kill_group(proc)
                return "shutdown"
            if now >= deadline:
                log.warning("job %s timed out after %.0fs; killing", jid, timeout)
                _kill_group(proc)
                return "timeout"
            if now >= next_beat:
                next_beat = now + self.heartbeat_seconds
                try:
                    if self.client.heartbeat(jid) == "cancelled":
                        _kill_group(proc)
                        return "cancelled"
                except Exception as exc:  # noqa: BLE001 - a bad heartbeat must not kill the job
                    log.warning("heartbeat for %s failed: %s", jid, exc)
            self.stop.wait(self.poll_interval)
        return "done"

    def upload(self, jid: str, payload: dict) -> None:
        delay = 1.0
        for attempt in range(1, self.upload_attempts + 1):
            try:
                status = self.client.result(jid, payload)
                if status == "dropped":
                    log.info("job %s: coordinator no longer expects a result (cancelled/expired)", jid)
                else:
                    log.info("job %s result accepted", jid)
                return
            except HttpStatus as exc:
                if payload.get("outcome") == "error" and attempt > 1:
                    log.error("job %s: coordinator rejected even the error result: %s", jid, exc)
                    return
                log.error("job %s: coordinator rejected the result: %s", jid, exc)
                payload = self._error_payload(f"the coordinator rejected the result: {exc.body[:500]}")
                continue
            except AuthError:
                log.error("job %s: token rejected while uploading", jid)
                return
            except CoordinatorError as exc:
                log.warning("job %s: upload attempt %d failed: %s", jid, attempt, exc)
                if self.stop.wait(delay):
                    delay = 0.2
                delay = min(delay * 2, 30.0)
        log.error("job %s: giving up uploading the result", jid)
