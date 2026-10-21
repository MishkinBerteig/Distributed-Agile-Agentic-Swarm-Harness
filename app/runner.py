"""DAASH host runner — spawns and supervises headless harness sessions.

The coordinator runs inside Docker and cannot launch processes on the host,
but harness CLIs (openclaude, claude, codex, ...) are installed *on devices*.
This daemon is that device's agent: it runs outside compose (`python3 -m
app.runner`) with the user's own CLI install and credentials, exposes a tiny
bearer-authenticated HTTP API on a published port, and spawns one headless
harness session per request.

Protocol for OpenClaude sessions (validated empirically):
    openclaude -p --input-format stream-json --output-format stream-json \
        --verbose --permission-mode fullAccess --model <m> \
        --system-prompt "<composed Vision→Mission prompt>"
The process stays alive on a persistent stdin; each newline-delimited JSON
user message is one turn, answered by events through `result`. stdout events
are kept in a bounded ring buffer so the coordinator can poll status and tail
activity without owning the pipes.

Standard library only: a host needs Python 3 and the harness CLI installed —
no pip install, no project venv (per AGENTS.md, nothing is built locally).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 7078
MAX_EVENTS = 500          # ring-buffer cap per session
DRAIN_TIMEOUT = 1.0       # seconds to wait for stdout drain after terminate

_sessions: dict[str, "Session"] = {}
_lock = threading.Lock()


class Session:
    """One headless harness process with a bounded event tail."""

    def __init__(self, spec: dict) -> None:
        self.id = str(spec.get("session_id") or uuid.uuid4())
        self.swarm_id = spec.get("swarm_id", "")
        self.team_id = spec.get("team_id", "")
        self.role = spec.get("role", "")
        self.harness = spec.get("harness", "openclaude")
        self.model = spec.get("model") or ""
        self.system_prompt = spec.get("system_prompt", "")
        self.workspace = spec["workspace"]
        self.state = "launching"      # launching | live | stopped | failed
        self.error: str | None = None
        self.harness_session_id: str | None = None   # from the init event
        self.events: collections.deque[dict] = collections.deque(maxlen=MAX_EVENTS)
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self.turns = 0
        self.created_at = time.time()
        self.process: subprocess.Popen | None = None
        self._stdin_lock = threading.Lock()

    # -- spawn / supervise -------------------------------------------------

    def build_argv(self) -> list[str]:
        argv = [
            self.harness, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "fullAccess",
            "--system-prompt", self.system_prompt,
        ]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def spawn(self) -> None:
        os.makedirs(self.workspace, exist_ok=True)
        try:
            self.process = subprocess.Popen(  # noqa: S603 — argv is operator-declared harness CLI
                self.build_argv(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, cwd=self.workspace, start_new_session=True,
            )
        except FileNotFoundError as exc:
            self.state = "failed"
            self.error = f"harness CLI not found on host: {exc}"
            return
        except OSError as exc:
            self.state = "failed"
            self.error = f"spawn failed: {exc}"
            return

        # A CLI that rejects its arguments exits within a few hundred ms, at
        # which point the stdout pump sees EOF and records stopped/failed. Wait
        # for that before declaring the session live so callers get an honest
        # 502 instead of a phantom live session. The model name stays unknown
        # until the init event arrives on the first turn — reconcile picks it
        # up as soon as it is known.
        self._out_thread.join(timeout=0.3)
        if self.state == "launching":
            self.state = "live"

        self._out_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._out_thread.start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        if os.environ.get("DAASH_RUNNER_KILL_TEST"):
            # Test-only: simulate a crashed agent so reconcile/failed paths are
            # exercisable without killing real processes.
            threading.Timer(0.25, self.terminate).start()

    def _pump_stdout(self) -> None:
        proc = self.process
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = {"type": "raw", "text": line[:2000]}
            self.events.append(event)
            if event.get("type") == "system" and event.get("subtype") == "init":
                sid = event.get("session_id")
                if isinstance(sid, str) and sid:
                    self.harness_session_id = sid
                model = event.get("model")
                if isinstance(model, str) and model:
                    self.model = model
            elif event.get("type") == "result":
                self.turns += 1
        rc = proc.wait()
        with _lock:
            if self.state in ("live", "launching"):
                self.state = "stopped" if rc == 0 else "failed"
                if rc != 0:
                    tail = "\n".join(self.stderr_tail)[-500:]
                    self.error = f"harness exited rc={rc}" + (f": {tail}" if tail else "")

    def _pump_stderr(self) -> None:
        proc = self.process
        assert proc is not None and proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                # Keep stderr visible but bounded — CLI diagnostics are useful
                # when a session fails (the ring buffer keeps only the tail).
                self.stderr_tail.append(line[:2000])
                self.events.append({"type": "stderr", "text": line[:2000]})

    # -- interaction ---------------------------------------------------------

    def send_turn(self, text: str) -> None:
        if self.state != "live" or not self.process or not self.process.stdin:
            raise RuntimeError(f"session {self.id} is {self.state}")
        msg = {"type": "user", "message": {"role": "user",
                                           "content": [{"type": "text", "text": text}]}}
        with self._stdin_lock:
            self.process.stdin.write(json.dumps(msg) + "\n")
            self.process.stdin.flush()

    def terminate(self) -> None:
        proc = self.process
        if not proc or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()

    def wait_exit(self, timeout: float = DRAIN_TIMEOUT) -> bool:
        # Joining the stdout pump requires EOF — terminate() closes pipes.
        proc = self.process
        if not proc:
            return True
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=timeout)
            return True

    def status_dict(self) -> dict:
        pid = self.process.pid if self.process and self.process.poll() is None else None
        return {
            "session_id": self.id,
            "swarm_id": self.swarm_id,
            "role": self.role,
            "harness": self.harness,
            "model": self.model or None,
            "state": self.state,
            "error": self.error,
            "pid": pid,
            "turns": self.turns,
            "harness_session_id": self.harness_session_id,
        }


# -- HTTP API --------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter than stdlib default
        sys.stderr.write("[runner] %s\n" % (fmt % args))

    # -- plumbing ------------------------------------------------------------

    @property
    def token(self) -> str:
        return os.environ.get("DAASH_RUNNER_TOKEN", "")

    def _authed(self) -> bool:
        expected = self.token
        if not expected:  # unset token => open runner (dev only; refuse otherwise below)
            return True
        got = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return secrets.compare_digest(got, expected)

    def _send(self, code: int, body: dict | list) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            raise ValueError("invalid JSON body")

    # -- routes ---------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        if path == "/health":
            with _lock:
                states = collections.Counter(s.state for s in _sessions.values())
            self._send(200, {"status": "ok", "sessions": dict(states)})
            return
        if not self._authed():
            self._send(401, {"detail": "invalid token"})
            return

        parts = [p for p in path.split("/") if p]
        # /sessions or /sessions/{id}(/events)
        if parts[:1] == ["sessions"]:
            with _lock:
                if len(parts) == 1:
                    self._send(200, [s.status_dict() for s in _sessions.values()])
                    return
                sess = _sessions.get(parts[1])
                if sess is None:
                    self._send(404, {"detail": "session not found"})
                    return
                if len(parts) == 2 or parts[2] == "events":
                    tail = sess.status_dict() | {
                        "events": list(sess.events), "created_at": sess.created_at}
                    self._send(200, tail)
                    return
        self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        if not self._authed():
            self._send(401, {"detail": "invalid token"})
            return
        try:
            body = self._json_body()
        except ValueError as exc:
            self._send(422, str(exc))
            return

        parts = [p for p in path.split("/") if p]
        if parts == ["sessions"]:
            self._create(body)
            return
        # /sessions/{id}/turns
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "turns":
            with _lock:
                sess = _sessions.get(parts[1])
            if sess is None:
                self._send(404, {"detail": "session not found"})
                return
            text = body.get("text", "")
            try:
                sess.send_turn(str(text))
            except RuntimeError as exc:
                self._send(409, str(exc))
                return
            self._send(202, {"accepted": True})
            return
        self._send(404, {"detail": "not found"})

    def do_DELETE(self) -> None:
        path = self.path.rstrip("/")
        if not self._authed():
            self._send(401, {"detail": "invalid token"})
            return
        parts = [p for p in path.split("/") if p]
        if len(parts) == 2 and parts[0] == "sessions":
            with _lock:
                sess = _sessions.get(parts[1])
            if sess is None:
                self._send(404, {"detail": "session not found"})
                return
            sess.terminate()
            sess.wait_exit(timeout=5.0)
            self._send(200, sess.status_dict())
            return
        self._send(404, {"detail": "not found"})

    def _create(self, body: dict) -> None:
        required = ("swarm_id", "role", "system_prompt")
        missing = [k for k in required if not str(body.get(k, "")).strip()]
        if missing:
            self._send(422, f"missing fields: {', '.join(missing)}")
            return
        spec = dict(body)
        spec.setdefault("workspace", os.path.expanduser(f"~/DAASH-workspaces/{body['swarm_id']}"))
        sess = Session(spec)
        with _lock:
            _sessions[sess.id] = sess
        sess.spawn()
        code = 201 if sess.state != "failed" else 502
        self._send(code, sess.status_dict())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.runner", description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("DAASH_RUNNER_HOST", "127.0.0.1"),
                        help="bind address (default 127.0.0.1; use 0.0.0.0 for cross-device)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("DAASH_RUNNER_PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)

    token = os.environ.get("DAASH_RUNNER_TOKEN", "")
    if not token:
        print("[runner] WARNING: DAASH_RUNNER_TOKEN unset — runner accepts unauthenticated "
              "requests. Set it for any non-local use.", file=sys.stderr)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[runner] listening on http://{args.host}:{args.port} — sessions land in ~/DAASH-workspaces")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with _lock:
            for s in _sessions.values():
                s.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
