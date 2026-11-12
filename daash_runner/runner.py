"""The runner loop: hello, lease, run jobs in worker threads, heartbeat, upload results.

On an agent host (docs/DESIGN-0.4.md 5.9) the runner also reports the machine's agents in every
hello (`agent_host.report()`), hands the hello answer back (`agent_host.handle_hello()`: new DAASH
tool tokens), and runs `connected` jobs by relaying them to one of those agents (daash_runner.relay)."""
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

from . import adapters, relay, workspace
from .client import AuthError, Client, CoordinatorError, HttpStatus
from .checks import run_checks
from .config import available_harnesses

VERSION = "0.4.0"
log = logging.getLogger("daash_runner")

SUMMARY_MAX = 4000
TAIL_MAX = 20000
PRUNE_AFTER_SECONDS = 3 * 24 * 3600
REHELLO_SECONDS = 300
CLEANUP_TIMEOUT = 30          # below the 60 s lease: no heartbeats while it runs
#: Harness of jobs for connected agents; an agent host relays them (docs/DESIGN-0.4.md 5.9).
CONNECTED_HARNESS = "connected"


def mcp_endpoint(job: dict, coordinator_url: str) -> tuple[str, str]:
    """(url, token) of the job's DAASH MCP server (docs/DESIGN-0.4.md 4.1), or ("", "").

    The lease names a path; the URL is built from the runner's own coordinator URL, so it works
    wherever the runner reaches the coordinator. A lease without `mcp` (older coordinator) or with
    anything but an absolute path means the job runs without DAASH tools."""
    mcp = job.get("mcp")
    if not isinstance(mcp, dict):
        return "", ""
    path, token = str(mcp.get("path") or ""), str(mcp.get("token") or "")
    if not token or not path.startswith("/") or path.startswith("//"):
        return "", ""
    return coordinator_url.rstrip("/") + path, token


def prepare_invocation(inv: adapters.Invocation) -> None:
    """Create the adapter's private folders and config files (owner-only)."""
    for d in inv.dirs:
        os.makedirs(d, mode=0o700, exist_ok=True)
    for path, content in inv.files.items():
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)


def finish_invocation(inv: adapters.Invocation, cwd: str) -> None:
    """After the CLI: run the adapter's cleanup commands (e.g. stop a Hermes gateway the CLI
    started in the background), then delete what it no longer needs. Never raises."""
    for argv in inv.cleanup:
        try:
            subprocess.run(argv, cwd=cwd, env=inv.env, stdin=subprocess.DEVNULL,  # noqa: S603
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=CLEANUP_TIMEOUT, start_new_session=True, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("cleanup command %s failed: %s", argv[:3], exc)
    for path in inv.remove_after:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


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
                 host: str | None = None, environ: dict | None = None,
                 agent_host: Any = None) -> None:
        self.client = client
        self.agent_host = agent_host      # daash_runner.host.AgentHost on an agent host
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
        agents = self.agent_host.report() if self.agent_host is not None else None
        info = self.client.hello(self.host, VERSION, self.max_jobs, available_harnesses(self.cfg),
                                 agents=agents)
        self.name = info.get("name", self.name)
        self.heartbeat_seconds = float(info.get("heartbeat_seconds") or self.heartbeat_seconds)
        if self.agent_host is not None:
            for line in self.agent_host.handle_hello(info):
                log.info("%s", line)
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
        what = (f"{job.get('relay')} '{job.get('local_ref')}'" if job.get("harness") == CONNECTED_HARNESS
                else f"{job.get('harness')}/{job.get('model')}")
        log.info("leased job %s (%s, task %s)", job["id"], what, job.get("task_id"))

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
        if job.get("harness") == CONNECTED_HARNESS:
            return self.execute_relay(job)
        jid = job["id"]
        jdir = os.path.join(self.jobs_dir, jid)
        ws = os.path.join(jdir, "ws")
        os.makedirs(jdir, exist_ok=True)
        harness = job.get("harness", "")
        hcfg = self.cfg["harnesses"].get(harness)
        if hcfg is None or harness not in adapters.ADAPTERS:
            return self._error_payload(f"this runner has no harness '{harness}' configured")
        started = time.monotonic()
        problem = self._fetch_workspace(job, jdir, ws)
        if problem is not None:
            return problem or None
        before = workspace.snapshot(ws)

        state = os.path.join(self.state_dir, harness)
        os.makedirs(state, exist_ok=True)
        task = job.get("task") or {}
        limits = job.get("limits") or {}
        timeout = float(min(limits.get("timeout_seconds") or hcfg["timeout_seconds"], hcfg["timeout_seconds"]))
        mcp_url, mcp_token = mcp_endpoint(job, self.client.base)
        spec = adapters.JobSpec(model=job.get("model", ""), system_prompt=job.get("system_prompt", ""),
                                prompt=job.get("prompt", ""), state_dir=state, artifact=task.get("artifact") or "",
                                job_dir=jdir, workdir=ws, timeout_seconds=int(timeout),
                                mcp_url=mcp_url, mcp_token=mcp_token)
        inv = adapters.build(harness, hcfg, spec, self.environ)
        ran = self._run_cli(jid, inv, ws, jdir, timeout)
        if isinstance(ran, dict):
            return ran
        state_flag, exit_code, stdout, stderr = ran
        if state_flag == "cancelled":
            log.info("job %s cancelled by the coordinator; dropped", jid)
            return None
        parsed = adapters.parse(harness, stdout, stderr, exit_code)
        return self._collect(job, parsed, state_flag, timeout, ws, before, inv.env, stdout, stderr,
                             exit_code, started)

    # -- the steps every job shares ---------------------------------------------------------

    def _fetch_workspace(self, job: dict, jdir: str, dest: str) -> dict | None:
        """Download and safely extract the swarm workspace into `dest`. None = done; an error
        payload; or {} when the job is no longer ours (drop it)."""
        jid = job["id"]
        tar_path = os.path.join(jdir, "workspace.tar.gz")
        try:
            self.client.download_workspace(jid, tar_path, job.get("workspace_url"))
            workspace.safe_extract(tar_path, dest)
        except workspace.UnsafeArchive as exc:
            return self._error_payload(f"unsafe workspace archive: {exc}")
        except HttpStatus as exc:
            if exc.status in (404, 409):
                log.info("job %s: workspace not available (%s), dropping", jid, exc.status)
                return {}
            return self._error_payload(f"workspace download failed: {exc}")
        finally:
            try:
                os.remove(tar_path)
            except OSError:
                pass
        return None

    def _run_cli(self, jid: str, inv: adapters.Invocation, cwd: str, jdir: str, timeout: float):
        """Start the CLI in its own process group and supervise it (heartbeats, timeout,
        cancel). Returns (state flag, exit code, stdout, stderr), or an error payload."""
        out_path, err_path = os.path.join(jdir, "stdout.log"), os.path.join(jdir, "stderr.log")
        try:
            prepare_invocation(inv)
        except OSError as exc:
            return self._error_payload(f"could not write the CLI's config: {exc}")
        try:
            with open(out_path, "wb") as out, open(err_path, "wb") as err:
                try:
                    proc = subprocess.Popen(  # noqa: S603 - argv is operator-configured harness CLI
                        inv.argv, cwd=cwd, env=inv.env, stdin=subprocess.PIPE, stdout=out, stderr=err,
                        start_new_session=True)
                except FileNotFoundError:
                    return self._error_payload(f"harness CLI '{inv.argv[0]}' not found on this runner")
                except OSError as exc:
                    return self._error_payload(f"could not start '{inv.argv[0]}': {exc}")
                with self._lock:
                    self._procs[jid] = proc
                threading.Thread(target=self._feed_stdin, args=(proc, inv.stdin), daemon=True).start()
                state_flag = self._supervise(jid, proc, timeout)
        finally:
            finish_invocation(inv, cwd)
            with self._lock:
                self._procs.pop(jid, None)
        return state_flag, proc.returncode, _tail(out_path, 20_000_000), _tail(err_path, 200_000)

    def _collect(self, job: dict, parsed: adapters.Parsed, state_flag: str, timeout: float, ws: str,
                 before: dict, env: dict | None, stdout: str, stderr: str, exit_code,
                 started: float) -> dict | None:
        """The result payload: outcome, changed files, command checks. None = cancelled during
        the checks (drop the job)."""
        jid = job["id"]
        limits = job.get("limits") or {}
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
            check_results = self._run_checks(jid, job["checks"], ws, env)
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

    # -- agent hosts: relay a job to one of the owner's agents (docs/DESIGN-0.4.md 5.9) ----------

    def execute_relay(self, job: dict) -> dict | None:
        """A `connected` job pinned to this agent host: put a copy of the workspace where the
        agent can work on it, hand it the job through its own CLI, then report the changes."""
        jid = job["id"]
        jdir = os.path.join(self.jobs_dir, jid)
        os.makedirs(jdir, exist_ok=True)
        relay_name, local_ref = str(job.get("relay") or ""), str(job.get("local_ref") or "")
        if self.agent_host is None:
            return self._error_payload("this runner is not an agent host (it has no local agents)")
        if relay_name not in relay.RELAYS:
            return self._error_payload(f"unknown relay '{relay_name}' (this runner knows "
                                       f"{', '.join(relay.RELAYS)})")
        agent = self.agent_host.resolve(relay_name, local_ref)
        if agent is None:
            return self._error_payload(f"agent '{local_ref}' ({relay_name}) is no longer on this machine")
        if not agent.available:
            return self._error_payload(f"{agent.name} is not available: {agent.detail}")
        try:
            folder = relay.job_folder(relay_name, agent, jdir, jid)
        except ValueError as exc:
            return self._error_payload(str(exc))
        started = time.monotonic()
        limits = job.get("limits") or {}
        timeout = float(limits.get("timeout_seconds") or 1800)
        remove_folder = relay_name == relay.OPENCLAW_RELAY
        try:
            if os.path.isdir(folder):
                shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder, exist_ok=True)
            problem = self._fetch_workspace(job, jdir, folder)
            if problem is not None:
                return problem or None
            before = workspace.snapshot(folder)
            text = relay.message(job, folder, self.agent_host.has_tools(agent))
            env = self.agent_host.env()
            if relay_name == relay.OPENCLAW_RELAY:
                msg_path = os.path.join(jdir, "message.md")
                fd = os.open(msg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                inv = relay.build_openclaw_agent(agent, job, msg_path, env, int(timeout))
            else:
                inv = relay.build_hermes_agent(agent, folder, env, text)
            log.info("job %s: relaying to %s (%s) in %s", jid, agent.name, relay_name, folder)
            ran = self._run_cli(jid, inv, folder, jdir, timeout)
            if isinstance(ran, dict):
                return ran
            state_flag, exit_code, stdout, stderr = ran
            if state_flag == "cancelled":
                log.info("job %s cancelled by the coordinator; dropped", jid)
                return None
            parsed = relay.parse(relay_name, stdout, stderr, exit_code)
            if parsed.outcome == "submitted":
                written, removed = relay.apply_file_blocks(parsed.final_text, folder)
                if written or removed:
                    log.info("job %s: applied %d file block(s) and %d deletion(s) from the reply",
                             jid, len(written), len(removed))
            return self._collect(job, parsed, state_flag, timeout, folder, before, inv.env, stdout,
                                 stderr, exit_code, started)
        finally:
            if remove_folder:
                shutil.rmtree(folder, ignore_errors=True)
                try:
                    os.rmdir(os.path.dirname(folder))      # daash-jobs/, when empty
                except OSError:
                    pass

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
