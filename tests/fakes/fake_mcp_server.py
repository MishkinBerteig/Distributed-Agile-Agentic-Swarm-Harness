"""A tiny stand-in for DAASH's MCP server (docs/DESIGN-0.4.md section 2.2), standard library only.

Streamable HTTP, stateless, JSON responses: `POST /mcp` takes a JSON-RPC message (or a batch) and
answers `initialize`, `notifications/*` (202, no body), `ping`, `tools/list` and `tools/call`;
`GET`/`DELETE /mcp` -> 405. A wrong or missing `Authorization: Bearer <token>` -> 401. Every request
is recorded (in memory and, with `--log`, as JSONL), so tests and the live CLI checks can see which
tools an agent listed and called.

    python3 -m tests.fakes.fake_mcp_server --port 9200 --token djt_test --log calls.jsonl
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


def _schema(props: dict[str, str], required: list[str]) -> dict:
    return {"type": "object", "properties": {k: {"type": v} for k, v in props.items()},
            "required": required}


TOOLS = [
    {"name": "remember", "description": "Save a note to your persistent DAASH memory.",
     "inputSchema": _schema({"content": "string"}, ["content"])},
    {"name": "recall", "description": "Search your DAASH memory.",
     "inputSchema": _schema({"query": "string"}, [])},
    {"name": "team_roster", "description": "List your team's agents.", "inputSchema": _schema({}, [])},
    {"name": "send_message", "description": "Send a message to a team member, 'team' or 'coordinator'.",
     "inputSchema": _schema({"to": "string", "text": "string"}, ["to", "text"])},
    {"name": "read_messages", "description": "Read your unread team messages.", "inputSchema": _schema({}, [])},
]


class FakeMCP:
    """State shared by the handler threads."""

    def __init__(self, token: str = "", log_path: str = "") -> None:
        self.token = token
        self.log_path = log_path
        self.calls: list[dict] = []        # every tools/call: {"name", "arguments"}
        self.requests: list[dict] = []     # every JSON-RPC message seen: {"method", "auth_ok"}
        self.memory: list[str] = []
        self.lock = threading.Lock()

    def record(self, entry: dict) -> None:
        entry = {"t": round(time.time(), 3), **entry}
        with self.lock:
            self.requests.append(entry)
            if self.log_path:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\n")

    # -- JSON-RPC ---------------------------------------------------------------

    def handle(self, msg: Any) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": -32600, "message": "invalid request"}}
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if "id" not in msg:                       # a notification: no response
            return None
        if method == "initialize":
            asked = str(params.get("protocolVersion") or "")
            version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[1]
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "daash", "version": "0.4-fake"},
                      "instructions": "DAASH tools: memory, team messages."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = self.call(str(params.get("name") or ""), params.get("arguments") or {})
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call(self, name: str, args: dict) -> dict:
        with self.lock:
            self.calls.append({"name": name, "arguments": args})
        if name == "remember":
            content = str(args.get("content") or "").strip()
            if not content:
                return _text("content is required", error=True)
            with self.lock:
                self.memory.append(content)
            return _text(f"Remembered (memory #{len(self.memory)}).")
        if name == "recall":
            q = str(args.get("query") or "").lower()
            hits = [m for m in self.memory if q in m.lower()]
            return _text("\n".join(hits) if hits else "No memories found.")
        if name == "team_roster":
            return _text("Delivery-1 (you), Delivery-2")
        if name == "send_message":
            if not args.get("to") or not args.get("text"):
                return _text("to and text are required", error=True)
            return _text(f"Message sent to {args['to']}.")
        if name == "read_messages":
            return _text("No unread messages.")
        return _text(f"Unknown tool '{name}'.", error=True)


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def make_handler(state: FakeMCP):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # noqa: D401 - quiet
            pass

        def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
                  extra: dict | None = None) -> None:
            self.send_response(status)
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _auth_ok(self) -> bool:
            return not state.token or self.headers.get("Authorization", "") == f"Bearer {state.token}"

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") == "/health":
                return self._send(200, b'{"ok": true}')
            state.record({"http": "GET", "path": self.path, "auth_ok": self._auth_ok()})
            self._send(405, b'{"error": "method not allowed"}', extra={"Allow": "POST"})

        def do_DELETE(self):  # noqa: N802
            state.record({"http": "DELETE", "path": self.path, "auth_ok": self._auth_ok()})
            self._send(405, b'{"error": "method not allowed"}', extra={"Allow": "POST"})

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            ok = self._auth_ok()
            try:
                msg = json.loads(raw or b"null")
            except ValueError:
                msg = None
            msgs = msg if isinstance(msg, list) else [msg]
            for m in msgs:
                entry = {"http": "POST", "path": self.path, "auth_ok": ok,
                         "method": m.get("method") if isinstance(m, dict) else None,
                         "ua": self.headers.get("User-Agent", "")}
                if isinstance(m, dict) and m.get("method") == "tools/call":
                    entry["tool"] = (m.get("params") or {}).get("name")
                    entry["arguments"] = (m.get("params") or {}).get("arguments")
                state.record(entry)
            if self.path.rstrip("/") != "/mcp":
                return self._send(404, b'{"error": "not found"}')
            if not ok:
                return self._send(401, b'{"error": "unauthorized"}', extra={"WWW-Authenticate": "Bearer"})
            if msg is None:
                return self._send(400, json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32700, "message": "parse error"}}).encode())
            replies = [r for r in (state.handle(m) for m in msgs) if r is not None]
            if not replies:
                return self._send(202)
            body = replies if isinstance(msg, list) else replies[0]
            self._send(200, json.dumps(body).encode())

    return Handler


def start(port: int = 0, token: str = "", log_path: str = "", host: str = "127.0.0.1"):
    """Start in a background thread; returns (server, state). `server.server_address[1]` is the port."""
    state = FakeMCP(token, log_path)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9200)
    ap.add_argument("--token", default="")
    ap.add_argument("--log", default="")
    a = ap.parse_args()
    state = FakeMCP(a.token, a.log)
    server = ThreadingHTTPServer((a.host, a.port), make_handler(state))
    print(f"fake MCP server on {a.host}:{a.port}/mcp", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
