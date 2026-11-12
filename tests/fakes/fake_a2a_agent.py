"""A scripted fake of an existing A2A 1.0 agent (an OpenClaw gateway's `a2a` channel) for DAASH's
connected-agent tests (docs/DESIGN-0.4.md sections 5.3, 5.5 and 5.8). No LLM.

It serves a public Agent Card at `/.well-known/agent-card.json` (advertising an unreachable LAN
address, like real gateways do, so DAASH's endpoint origin rule is exercised) and JSON-RPC at
`/a2a/v1`: `SendMessage` (with `configuration.returnImmediately`), `GetTask`, `CancelTask`
(refused with -32004, like OpenClaw). Every JSON-RPC call needs the peer token; until the owner
"approves" (`approve()`, or `POST /_control/approve` over HTTP) there is none, so DAASH's check
gets HTTP 401, exactly like a gateway whose owner has not applied DAASH's config patch yet.

When a task arrives it works in the background, the way an agent with DAASH's MCP server would:

* mode `mcp` (default): reads the DAASH job id from the message, calls DAASH's MCP tools
  (`current_task`, `list_files`, `write_file` for each file the deterministic criteria name,
  `read_file`, `remember`, `send_message` to the coordinator), then completes with a summary;
* mode `blocks`: no MCP at all; the reply carries the files as `<<<FILE path>>>` blocks;
* mode `fail`: the task ends TASK_STATE_FAILED;
* mode `forget`: the first GetTask answers "task not found" once (as after a gateway restart),
  then it works like `mcp`.

    python -m tests.fakes.fake_a2a_agent --port 18789 --name Jane      # e2e container

In-process (integration tier): `FakeA2AAgent(...).httpx_handler` for `httpx.MockTransport`, with
`mcp_transport` pointing at DAASH's app (httpx.ASGITransport).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

import httpx

from tests.fakes.fake_llm import build_file_content, parse_criteria

JOB_RE = re.compile(r"Your DAASH job id: ([0-9a-fA-F-]{8,})")
#: The start of OpenClaw 2026.11.3's Control UI page (GET /), which says what the gateway is.
CONTROL_UI = ('<!doctype html>\n<html data-openclaw-control-ui-base-path="" lang="en">\n  <head>'
              '<script id="openclaw-control-ui-build-info">globalThis.OPENCLAW_CONTROL_UI_BUILD_'
              'INFO={"version":"2026.11.3","commit":"fake"}</script>')


def _needs(criteria: list[dict]) -> dict[str, dict]:
    """path -> what its content must satisfy (file_exists / min_words / file_contains)."""
    out: dict[str, dict] = {}
    for c in criteria:
        if c["kind"] in ("file_exists", "min_words", "file_contains", "json_valid") and c["path"]:
            n = out.setdefault(c["path"], {"contains": [], "min_words": 0, "json": False})
            if c["kind"] == "min_words":
                n["min_words"] = max(n["min_words"], int(c["arg"] or 0))
            elif c["kind"] == "file_contains" and c["arg"]:
                n["contains"].append(c["arg"])
            elif c["kind"] == "json_valid":
                n["json"] = True
    return out


@dataclass
class FakeTask:
    id: str
    context_id: str
    text: str
    state: str = "TASK_STATE_WORKING"
    reply: str = ""
    polls: int = 0
    log: list[str] = field(default_factory=list)


class FakeA2AAgent:
    def __init__(self, name: str = "Jane", *, mode: str = "mcp", token: Optional[str] = None,
                 mcp_url: str = "", mcp_token: str = "",
                 mcp_transport: Optional[httpx.AsyncBaseTransport] = None,
                 advertised: str = "http://10.9.9.9:18789", work_delay: float = 0.0,
                 channel_on: bool = True) -> None:
        self.name = name
        #: False = like an OpenClaw gateway before its owner turned the a2a channel on: no card,
        #: no /a2a/v1 (HTTP 404); approve() turns it on
        self.channel_on = channel_on
        self.mode = mode
        self.token = token
        self.mcp_url = mcp_url
        self.mcp_token = mcp_token
        self.mcp_transport = mcp_transport
        self.advertised = advertised
        self.work_delay = work_delay
        self.tasks: dict[str, FakeTask] = {}
        self.requests: list[dict] = []          # {"method", "auth"} of every JSON-RPC call
        self.tool_calls: list[tuple[str, dict, str]] = []   # (tool, args, result text)
        self.background: list[asyncio.Task] = []
        self.forgot = False
        self.lock = threading.Lock()

    # -- the owner's approval ---------------------------------------------------------------

    def approve(self, token: str, mcp_url: str = "", mcp_token: str = "") -> None:
        """What DAASH's approval step does to a real gateway: DAASH becomes a trusted peer with
        `token`, and the agent gets DAASH's MCP server."""
        self.token = token
        self.channel_on = True
        if mcp_url:
            self.mcp_url = mcp_url
        if mcp_token:
            self.mcp_token = mcp_token

    def card(self) -> dict:
        return {
            "name": f"{self.name}'s gateway",
            "description": "OpenClaw gateway (a scripted fake for DAASH tests)",
            "version": "2026.11.3-fake",
            "provider": {"organization": "OpenClaw", "url": "https://openclaw.ai"},
            "supportedInterfaces": [{"url": f"{self.advertised}/a2a/v1",
                                     "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "skills": [{"id": "main", "name": self.name, "description": "The main agent",
                        "tags": ["openclaw"]}]}

    # -- HTTP-independent core ------------------------------------------------------------------

    async def handle(self, method: str, path: str, headers: dict, body: bytes
                     ) -> tuple[int, Any]:
        if method == "GET" and path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            return (200, self.card()) if self.channel_on else (404, {"error": "Not Found"})
        if method == "GET" and path in ("/health", "/healthz"):
            return 200, {"ok": True, "status": "live"}
        if method == "GET" and path == "/":
            return 200, CONTROL_UI
        if path != "/a2a/v1" or method != "POST" or not self.channel_on:
            return 404, {"error": "not found"}
        auth = headers.get("authorization", "")
        ok = bool(self.token) and auth == f"Bearer {self.token}"
        try:
            msg = json.loads(body or b"{}")
        except ValueError:
            return 200, _rpc_error(None, -32700, "Parse error")
        self.requests.append({"method": msg.get("method"), "auth": ok})
        if not ok:
            return 401, {"error": "Unauthorized"}
        mid = msg.get("id")
        m = msg.get("method")
        params = msg.get("params") or {}
        if m in ("SendMessage", "message/send"):
            return 200, self._send(mid, params)
        if m in ("GetTask", "tasks/get"):
            t = self.tasks.get(str(params.get("id")))
            if t is None:
                return 200, _rpc_error(mid, -32001, "Task not found")
            t.polls += 1
            if self.mode == "forget" and not self.forgot:
                self.forgot = True          # like a gateway restart: once
                del self.tasks[t.id]
                return 200, _rpc_error(mid, -32001, "Task not found")
            return 200, {"jsonrpc": "2.0", "id": mid, "result": self._task_json(t)}
        if m in ("CancelTask", "tasks/cancel"):
            return 200, _rpc_error(mid, -32004, "Task cannot be canceled")
        return 200, _rpc_error(mid, -32601, f"Method not found: {m}")

    def _send(self, mid: Any, params: dict) -> dict:
        message = params.get("message") or {}
        text = "\n".join(p.get("text", "") for p in message.get("parts") or []
                         if isinstance(p, dict))
        t = FakeTask(id=str(uuid.uuid4()), context_id=str(message.get("contextId") or ""),
                     text=text)
        self.tasks[t.id] = t
        coro = self._work(t)
        try:
            loop = asyncio.get_running_loop()
            self.background.append(loop.create_task(coro))
        except RuntimeError:   # no loop (threaded HTTP server): a thread of its own
            threading.Thread(target=asyncio.run, args=(coro,), daemon=True).start()
        return {"jsonrpc": "2.0", "id": mid, "result": {"task": self._task_json(t)}}

    def _task_json(self, t: FakeTask) -> dict:
        out: dict[str, Any] = {"id": t.id, "contextId": t.context_id,
                               "status": {"state": t.state,
                                          "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ")},
                               "history": []}
        if t.state == "TASK_STATE_COMPLETED":
            out["artifacts"] = [{"artifactId": f"a-{t.id[:8]}", "parts": [{"text": t.reply}]}]
        elif t.state == "TASK_STATE_FAILED":
            out["status"]["message"] = {"role": "ROLE_AGENT", "messageId": f"m-{t.id[:8]}",
                                        "parts": [{"text": t.reply}]}
        return out

    # -- the work -------------------------------------------------------------------------------

    async def _work(self, t: FakeTask) -> None:
        try:
            if self.work_delay:
                await asyncio.sleep(self.work_delay)
            if self.mode == "fail":
                t.reply = "I could not do this task: the scripted agent always fails."
                t.state = "TASK_STATE_FAILED"
                return
            criteria = parse_criteria(t.text)
            needs = _needs(criteria)
            title = (re.search(r"^Task [^:]*: (.+)$", t.text, re.M) or [None, "Task"])[1]
            if self.mode == "blocks":
                blocks = "".join(f"<<<FILE {p}>>>\n{build_file_content(p, n, title)}"
                                 f"<<<END FILE>>>\n" for p, n in needs.items())
                t.reply = (f"I wrote {', '.join(needs)} as requested.\n\n{blocks}\n"
                           "Each criterion is met: the files exist with an Overview.")
                t.state = "TASK_STATE_COMPLETED"
                return
            job = (JOB_RE.search(t.text) or [None, None])[1]
            await self._mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "fake-a2a", "version": "1"}})
            await self._mcp("tools/list", {})
            await self.tool("current_task", {"job": job})
            await self.tool("list_files", {"job": job})
            for path, n in needs.items():
                await self.tool("write_file", {"job": job, "path": path,
                                               "content": build_file_content(path, n, title)})
                await self.tool("read_file", {"job": job, "path": path})
            await self.tool("remember", {"job": job, "content": f"{self.name} wrote "
                                         f"{', '.join(needs) or 'nothing'} for '{title}'."})
            await self.tool("send_message", {"job": job, "to": "coordinator",
                                             "text": f"{self.name} here: '{title}' is done."})
            t.reply = (f"Done. I wrote {', '.join(needs)} with DAASH's write_file tool; each "
                       "file exists, has an Overview and more than 20 words.")
            t.state = "TASK_STATE_COMPLETED"
        except Exception as exc:  # noqa: BLE001 - a broken script is a failed task
            t.reply = f"The scripted agent crashed: {type(exc).__name__}: {exc}"
            t.state = "TASK_STATE_FAILED"

    async def _mcp(self, method: str, params: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        async with httpx.AsyncClient(transport=self.mcp_transport, timeout=30) as client:
            r = await client.post(self.mcp_url, json=body,
                                  headers={"Authorization": f"Bearer {self.mcp_token}",
                                           "Accept": "application/json, text/event-stream"})
        r.raise_for_status()
        return r.json()

    async def tool(self, name: str, args: dict) -> str:
        args = {k: v for k, v in args.items() if v is not None}
        res = (await self._mcp("tools/call", {"name": name, "arguments": args})).get("result") or {}
        text = "".join(c.get("text", "") for c in res.get("content") or [])
        with self.lock:
            self.tool_calls.append((name, args, text))
        if res.get("isError"):
            raise RuntimeError(f"{name} failed: {text}")
        return text

    # -- transports -------------------------------------------------------------------------

    async def httpx_handler(self, request: httpx.Request) -> httpx.Response:
        status, body = await self.handle(request.method, request.url.path,
                                         {k.lower(): v for k, v in request.headers.items()},
                                         request.content)
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"Content-Type": "text/html"})
        return httpx.Response(status, json=body)

    def state(self) -> dict:
        return {"approved": bool(self.token), "tasks": [
            {"id": t.id, "state": t.state, "context_id": t.context_id} for t in
            self.tasks.values()], "tool_calls": [c[0] for c in self.tool_calls],
            "requests": len(self.requests)}


class FakeHermesAPI(FakeA2AAgent):
    """The same scripted agent behind a Hermes Agent API server (v0.23 shapes, verified against
    a real one): public `GET /health` {"status", "platform": "hermes-agent", "version"},
    Bearer-key `GET /v1/models`, `POST /v1/runs` {input, session_id} -> 202 {run_id, status},
    `GET /v1/runs/{id}` {status: queued|running|completed|failed|cancelled, output, usage},
    `POST /v1/runs/{id}/stop`. `token` is the API_SERVER_KEY (None = API server off: 404)."""

    STATES = {"TASK_STATE_WORKING": "running", "TASK_STATE_COMPLETED": "completed",
              "TASK_STATE_FAILED": "failed"}

    def __init__(self, name: str = "Hermes", **kw: Any) -> None:
        super().__init__(name, **kw)
        self.stopped: list[str] = []
        self.sessions: list[str] = []

    async def handle(self, method: str, path: str, headers: dict, body: bytes
                     ) -> tuple[int, Any]:
        if not self.token:
            return 404, {"error": "not found"}
        if method == "GET" and path in ("/health", "/v1/health"):
            return 200, {"status": "ok", "platform": "hermes-agent", "version": "0.23.1"}
        ok = headers.get("authorization", "") == f"Bearer {self.token}"
        self.requests.append({"method": f"{method} {path}", "auth": ok})
        if not ok:
            return 401, {"error": {"message": "Invalid gateway API key (API_SERVER_KEY)",
                                   "type": "gateway_auth_error", "code": "gateway_auth_failed"}}
        if method == "GET" and path == "/v1/models":
            return 200, {"object": "list", "data": [{"id": "hermes-agent", "object": "model"}]}
        if method == "POST" and path == "/v1/runs":
            req = json.loads(body or b"{}")
            self.sessions.append(str(req.get("session_id") or ""))
            t = FakeTask(id=f"run_{uuid.uuid4().hex}", context_id=str(req.get("session_id")),
                         text=str(req.get("input") or ""))
            self.tasks[t.id] = t
            self.background.append(asyncio.get_running_loop().create_task(self._work(t)))
            return 202, {"run_id": t.id, "status": "started", "replayed": False}
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["v1", "runs"]:
            t = self.tasks.get(parts[2])
            if t is None:
                return 404, {"error": {"message": "Run not found", "code": "run_not_found"}}
            if len(parts) == 4 and parts[3] == "stop" and method == "POST":
                self.stopped.append(t.id)
                t.state, t.reply = "TASK_STATE_FAILED", "stopped"
                return 200, {"run_id": t.id, "status": "stopping"}
            status = self.STATES.get(t.state, "running")
            if t.id in self.stopped:
                status = "cancelled"
            return 200, {"object": "hermes.run", "run_id": t.id, "status": status,
                         "session_id": t.context_id,
                         "output": t.reply if status == "completed" else "",
                         "error": t.reply if status == "failed" else None,
                         "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}
        return 404, {"error": "not found"}


def _rpc_error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(agent: FakeA2AAgent, port: int) -> ThreadingHTTPServer:
    """A stdlib HTTP server for the e2e container, plus /_control/approve and /_control/state."""
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def _go(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.split("?", 1)[0]
            if path == "/_control/approve" and method == "POST":
                cfg = json.loads(body or b"{}")
                agent.approve(cfg.get("token", ""), cfg.get("mcp_url", ""),
                              cfg.get("mcp_token", ""))
                if cfg.get("mode"):
                    agent.mode = cfg["mode"]
                status, out = 200, agent.state()
            elif path == "/_control/state":
                status, out = 200, agent.state()
            else:
                fut = asyncio.run_coroutine_threadsafe(agent.handle(
                    method, path, {k.lower(): v for k, v in self.headers.items()}, body), loop)
                status, out = fut.result(timeout=60)
            html_page = isinstance(out, str)
            data = out.encode() if html_page else json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html" if html_page else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._go("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._go("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            pass

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=18789)
    ap.add_argument("--name", default="Jane")
    ap.add_argument("--mode", default="mcp", choices=["mcp", "blocks", "fail", "forget"])
    args = ap.parse_args()
    server = serve(FakeA2AAgent(args.name, mode=args.mode), args.port)
    print(f"fake A2A agent {args.name} on :{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
