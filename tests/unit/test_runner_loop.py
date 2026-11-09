"""The runner loop against a tiny in-test coordinator stub (http.server in a thread)."""
import io
import json
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from daash_runner.client import AuthError, Client
from daash_runner.runner import Runner, prune_jobs

TOKEN = "daash_rt_test"


class Stub:
    def __init__(self):
        self.jobs: list[dict] = []
        self.hellos: list[dict] = []
        self.results: dict[str, dict] = {}
        self.heartbeats: list[str] = []
        self.heartbeat_reply = "leased"
        self.fail_hello = 0
        self.fail_lease = 0
        self.lease_calls = 0
        self.lock = threading.Lock()
        self.workspace_files = {"README.md": b"# shared\n", "old.md": b"old\n"}


def make_server(stub: Stub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=None, ctype="application/json"):
            data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
            self.send_response(code)
            if data:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _auth(self):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._send(401, {"detail": "bad token"})
                return False
            return True

        def do_GET(self):
            if not self._auth():
                return
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for name, data in stub.workspace_files.items():
                    ti = tarfile.TarInfo(name)
                    ti.size = len(data)
                    tf.addfile(ti, io.BytesIO(data))
            self._send(200, buf.getvalue(), "application/gzip")

        def do_POST(self):
            if not self._auth():
                return
            body = self._body()
            path = self.path
            with stub.lock:
                if path == "/runner/hello":
                    if stub.fail_hello > 0:
                        stub.fail_hello -= 1
                        return self._send(500, {"detail": "boom"})
                    stub.hellos.append(body)
                    return self._send(200, {"runner_id": "r1", "name": "stub", "poll_seconds": 1,
                                            "heartbeat_seconds": 0.05, "lease_seconds": 60})
                if path == "/runner/lease":
                    stub.lease_calls += 1
                    if stub.fail_lease > 0:
                        stub.fail_lease -= 1
                        return self._send(503, {"detail": "down"})
                    if stub.jobs and body.get("free_slots", 0) > 0:
                        return self._send(200, {"job": stub.jobs.pop(0)})
                    return self._send(204)
                if path.endswith("/heartbeat"):
                    stub.heartbeats.append(path.split("/")[3])
                    return self._send(200, {"status": stub.heartbeat_reply})
                if path.endswith("/result"):
                    stub.results[path.split("/")[3]] = body
                    return self._send(200, {"status": "completed"})
            self._send(404, {"detail": "nope"})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv


def job(jid="j1", timeout=60, **extra):
    d = {"id": jid, "swarm_id": "s", "task_id": "t1", "attempt": 0, "harness": "fake", "model": "fast",
         "system_prompt": "sys", "prompt": "Write it.\nArtifact: guides/basil.md",
         "limits": {"timeout_seconds": timeout, "max_files": 200, "max_file_bytes": 2000000,
                    "max_total_bytes": 20000000},
         "task": {"name": "Basil", "artifact": "guides/basil.md"}, "workspace_url": f"/runner/jobs/{jid}/workspace"}
    d.update(extra)
    return d


@pytest.fixture
def stub():
    s = Stub()
    srv = make_server(s)
    s.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield s
    srv.shutdown()
    srv.server_close()


def start_runner(stub, tmp_path, mode="ok", max_jobs=1, token=TOKEN, extra_env=None):
    env = {"DAASH_FAKE_MODE": mode, "DAASH_FAKE_SLOW_SECONDS": "0.1"}
    env.update(extra_env or {})
    cfg = {"harnesses": {"fake": {"command": sys.executable, "models": [], "env": env, "args": [],
                                  "timeout_seconds": 60}}}
    r = Runner(Client(stub.url, token, timeout=5), cfg, str(tmp_path / "work"), max_jobs, host="testhost", environ={})
    r.lease_wait = 0
    r.initial_backoff = 0.02
    r.max_backoff = 0.05
    r.poll_interval = 0.02
    r.idle_wait = 0.05
    r.upload_attempts = 3
    t = threading.Thread(target=r.run, daemon=True)
    t.start()
    return r, t


def stop_runner(r, t):
    r.stop.set()
    t.join(timeout=40)
    assert not t.is_alive()


def wait_for(cond, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_one_job_end_to_end(stub, tmp_path):
    stub.jobs.append(job())
    r, t = start_runner(stub, tmp_path)
    try:
        assert wait_for(lambda: "j1" in stub.results)
    finally:
        stop_runner(r, t)
    res = stub.results["j1"]
    assert res["outcome"] == "submitted", res
    assert [f["path"] for f in res["files"]] == ["guides/basil.md"]
    import base64
    assert len(base64.b64decode(res["files"][0]["content_b64"]).split()) >= 120
    assert res["deleted"] == [] and res["exit_code"] == 0 and res["summary"]
    assert res["usage"]["total_tokens"] > 0
    hello = stub.hellos[0]
    assert hello["host"] == "testhost" and hello["max_jobs"] == 1
    assert hello["harnesses"] == [{"harness": "fake", "models": [],
                                   "capabilities": ["edit_files", "run_code"]}]
    # the job folder is kept, with the extracted workspace and logs
    assert (tmp_path / "work" / "jobs" / "j1" / "ws" / "README.md").exists()
    assert (tmp_path / "work" / "jobs" / "j1" / "stdout.log").exists()


def test_modified_existing_file_is_reported(stub, tmp_path):
    stub.jobs.append(job(prompt="x"))
    r, t = start_runner(stub, tmp_path, extra_env={"DAASH_FAKE_ARTIFACT": "README.md"})
    try:
        assert wait_for(lambda: "j1" in stub.results)
    finally:
        stop_runner(r, t)
    assert [f["path"] for f in stub.results["j1"]["files"]] == ["README.md"]


def test_failed_and_error_outcomes(stub, tmp_path):
    for mode, expected in (("fail", "failed"), ("error", "error")):
        stub.results.clear()
        stub.jobs.append(job(f"j-{mode}"))
        r, t = start_runner(stub, tmp_path, mode)
        try:
            assert wait_for(lambda: f"j-{mode}" in stub.results)
        finally:
            stop_runner(r, t)
        res = stub.results[f"j-{mode}"]
        assert res["outcome"] == expected and res["error"] and res["files"] == []


def test_cancelled_heartbeat_kills_hanging_job(stub, tmp_path):
    stub.jobs.append(job())
    stub.heartbeat_reply = "cancelled"
    r, t = start_runner(stub, tmp_path, "hang")
    try:
        assert wait_for(lambda: stub.heartbeats)
        assert wait_for(lambda: r.active_count() == 0)
    finally:
        stop_runner(r, t)
    assert stub.results == {}          # cancelled jobs report nothing
    assert not r._procs                # the process group is gone


def test_timeout_reports_failed(stub, tmp_path):
    stub.jobs.append(job(timeout=0.4))
    r, t = start_runner(stub, tmp_path, "hang")
    try:
        assert wait_for(lambda: "j1" in stub.results)
    finally:
        stop_runner(r, t)
    assert stub.results["j1"]["outcome"] == "failed" and "timed out" in stub.results["j1"]["error"]


def test_shutdown_reports_error_for_running_job(stub, tmp_path):
    stub.jobs.append(job())
    r, t = start_runner(stub, tmp_path, "hang")
    assert wait_for(lambda: r.active_count() == 1 and r._procs)
    stop_runner(r, t)
    assert stub.results["j1"]["outcome"] == "error"
    assert "shut down" in stub.results["j1"]["error"]


def test_coordinator_5xx_backs_off_without_crashing(stub, tmp_path):
    stub.fail_hello = 2
    stub.fail_lease = 2
    stub.jobs.append(job())
    r, t = start_runner(stub, tmp_path)
    try:
        assert wait_for(lambda: "j1" in stub.results)
        assert t.is_alive()
    finally:
        stop_runner(r, t)
    assert stub.fail_hello == 0 and stub.fail_lease == 0


def test_coordinator_down_then_up(tmp_path):
    s = Stub()
    s.url = "http://127.0.0.1:9"      # nothing listens here
    r, t = start_runner(s, tmp_path)
    time.sleep(0.2)
    assert t.is_alive()
    stop_runner(r, t)


def test_bad_token_keeps_running_but_does_not_lease(stub, tmp_path):
    stub.jobs.append(job())
    r, t = start_runner(stub, tmp_path, token="wrong")
    time.sleep(0.2)
    assert t.is_alive() and stub.results == {} and len(stub.jobs) == 1
    stop_runner(r, t)
    with pytest.raises(AuthError):
        Client(stub.url, "wrong").hello("h", "v", 1, [])


def test_unconfigured_harness_reports_error(stub, tmp_path):
    stub.jobs.append(job(harness="codex"))
    r, t = start_runner(stub, tmp_path)
    try:
        assert wait_for(lambda: "j1" in stub.results)
    finally:
        stop_runner(r, t)
    assert stub.results["j1"]["outcome"] == "error"


def test_result_limits_turn_into_failed(stub, tmp_path):
    j = job()
    j["limits"]["max_file_bytes"] = 50
    stub.jobs.append(j)
    r, t = start_runner(stub, tmp_path)
    try:
        assert wait_for(lambda: "j1" in stub.results)
    finally:
        stop_runner(r, t)
    res = stub.results["j1"]
    assert res["outcome"] == "failed" and "per-file limit" in res["error"] and res["files"] == []


def test_prune_old_job_folders(tmp_path):
    jobs = tmp_path / "jobs"
    (jobs / "old").mkdir(parents=True)
    (jobs / "new").mkdir()
    now = time.time()
    import os
    os.utime(jobs / "old", (now - 4 * 86400, now - 4 * 86400))
    assert prune_jobs(str(jobs)) == 1
    assert not (jobs / "old").exists() and (jobs / "new").exists()
