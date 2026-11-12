"""Deterministic fake agent CLI for tests (`python3 -m daash_runner.fake_cli`).

Reads the prompt (last positional argument, or stdin), finds the task artifact path
(env DAASH_FAKE_ARTIFACT, else a line `Artifact: <path>`, else the first `*.md` path
mentioned), writes >= 120 words of plausible markdown there, and prints stream-json
lines ending with a Claude-Code-style `result` event.

DAASH_FAKE_MODE: ok | fail | error | hang | slow   (slow sleeps DAASH_FAKE_SLOW_SECONDS, default 3)

DAASH tools over MCP (docs/DESIGN-0.4.md 4.2): with DAASH_FAKE_MCP set to a comma list of
`remember`, `send_message` (or `all`) and the job's DAASH_MCP_URL / DAASH_MCP_TOKEN, the fake agent
calls those tools over streamable HTTP JSON-RPC (initialize, notifications/initialized, tools/call)
before it writes the artifact. `send_message` goes to DAASH_FAKE_MCP_TO (default `team`). The
outcome of each call is reported in the summary; DAASH_FAKE_MCP_STRICT=1 turns a failed call into a
failed run.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

_ARTIFACT_LINE = re.compile(r"Artifact\s*[:=]\s*[`'\"]?([^\s`'\"]+)", re.I)
_MD_PATH = re.compile(r"[`'\"(\s]([\w][\w./-]*\.md)\b")


def find_artifact(prompt: str, env_value: str = "") -> str:
    if env_value:
        return env_value
    m = _ARTIFACT_LINE.search(prompt)
    if m:
        return m.group(1).rstrip(".,;)")
    m = _MD_PATH.search(" " + prompt)
    return m.group(1) if m else "deliverable.md"


def make_content(path: str, prompt: str) -> str:
    title = os.path.splitext(os.path.basename(path))[0].replace("-", " ").replace("_", " ").title() or "Deliverable"
    paras = [
        f"# {title}",
        "## Overview",
        "This document was produced by the deterministic fake agent used to exercise the DAASH runner "
        "protocol end to end. It is plain markdown with several sections, so that simple automated checks "
        "such as a minimum word count, the presence of headings and the absence of placeholder text all pass.",
        "## Details",
        "The runner copies the shared swarm workspace to a private folder, starts this command inside it, "
        "waits for it to finish, and uploads every file whose content changed. Each step is logged, and the "
        "heartbeat keeps the lease alive while the command runs. A cancelled lease stops the command at once.",
        "## Steps",
        "1. Read the task and its acceptance criteria from the prompt.\n"
        "2. Write the requested artifact into the working directory.\n"
        "3. Reply with a short summary describing what was done and how each criterion is met.",
        "## Summary",
        "The artifact meets the stated criteria: it has a clear title, structured sections, concrete steps and "
        "a closing summary, and it contains no unfinished markers. Reviewers can verify it by reading the "
        "headings above and counting the words in the body.",
    ]
    return "\n\n".join(paras) + "\n"


def _emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


class MCPError(Exception):
    pass


class MCPClient:
    """Minimal MCP client over streamable HTTP (JSON or single-event SSE responses), stdlib only."""

    def __init__(self, url: str, token: str, timeout: float = 30.0) -> None:
        self.url, self.token, self.timeout = url, token, timeout
        self.session = ""
        self._id = 0

    def _post(self, msg: dict) -> dict | None:
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("MCP-Protocol-Version", "2025-06-18")
        if self.session:
            req.add_header("Mcp-Session-Id", self.session)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - job-given URL
                self.session = r.headers.get("Mcp-Session-Id") or self.session
                body = r.read().decode("utf-8", "replace")
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise MCPError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MCPError(str(exc)) from exc
        if "id" not in msg or not body.strip():
            return None
        if "text/event-stream" in ctype:
            datas = [ln[5:].strip() for ln in body.splitlines() if ln.startswith("data:")]
            body = datas[-1] if datas else ""
        try:
            reply = json.loads(body)
        except ValueError as exc:
            raise MCPError(f"bad JSON from the MCP server: {body[:200]}") from exc
        if isinstance(reply, dict) and reply.get("error"):
            raise MCPError(str(reply["error"].get("message") or reply["error"]))
        return reply.get("result") if isinstance(reply, dict) else None

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        return self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}) or {}

    def initialize(self) -> dict:
        info = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "daash-fake-cli", "version": "0.4"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return info

    def call_tool(self, name: str, arguments: dict) -> tuple[bool, str]:
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        text = " ".join(str(c.get("text") or "") for c in res.get("content") or [] if isinstance(c, dict))
        return (not res.get("isError")), text.strip()


def mcp_calls(wanted: str, artifact: str, environ=None) -> tuple[list[str], bool]:
    """Call the DAASH tools named in DAASH_FAKE_MCP. Returns (notes for the summary, all_ok)."""
    env = os.environ if environ is None else environ
    names = {n.strip() for n in wanted.split(",") if n.strip()}
    if names & {"1", "all", "true", "yes"}:
        names = {"remember", "send_message"}
    url, token = env.get("DAASH_MCP_URL", ""), env.get("DAASH_MCP_TOKEN", "")
    if not names:
        return [], True
    if not url or not token:
        return ["DAASH tools: no MCP server for this job."], False
    calls = []
    if "remember" in names:
        calls.append(("remember", {"content": f"Fake agent note: wrote {artifact} for this task."}))
    if "send_message" in names:
        calls.append(("send_message", {"to": env.get("DAASH_FAKE_MCP_TO", "team"),
                                       "text": f"I am writing {artifact}; it should be ready soon."}))
    notes, all_ok = [], True
    client = MCPClient(url, token)
    try:
        client.initialize()
    except MCPError as exc:
        return [f"DAASH tools: MCP initialize failed: {exc}"], False
    for name, args in calls:
        _emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": f"mcp__daash__{name}", "input": args}]}})
        try:
            ok, text = client.call_tool(name, args)
        except MCPError as exc:
            ok, text = False, str(exc)
        _emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": text, "is_error": not ok}]}})
        notes.append(f"DAASH {name}: {'ok' if ok else 'failed'} ({text[:120]})")
        all_ok = all_ok and ok
    return notes, all_ok


def main(argv: list[str]) -> int:
    prompt = ""
    # `--flag value` pairs: drop the values of known flags
    skip = False
    pos = []
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("--model", "--system-prompt"):
            skip = True
            continue
        if a.startswith("--"):
            continue
        pos.append(a)
    if pos:
        prompt = pos[-1]
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()

    mode = os.environ.get("DAASH_FAKE_MODE", "ok")
    _emit({"type": "system", "subtype": "init", "model": "fake", "cwd": os.getcwd()})

    if mode == "hang":
        while True:
            time.sleep(1)
    if mode == "slow":
        time.sleep(float(os.environ.get("DAASH_FAKE_SLOW_SECONDS", "3")))
    if mode == "error":
        text = "API Error: 503 service unavailable: model gateway unreachable (connection refused)"
        _emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": text,
               "usage": {"input_tokens": 0, "output_tokens": 0}, "total_cost_usd": 0})
        print(text, file=sys.stderr)
        return 1
    if mode == "fail":
        _emit({"type": "result", "subtype": "error_max_turns", "is_error": True,
               "result": "I could not finish the task within the allowed turns.",
               "usage": {"input_tokens": 200, "output_tokens": 20}, "total_cost_usd": 0})
        return 1

    path = find_artifact(prompt, os.environ.get("DAASH_FAKE_ARTIFACT", ""))
    notes, mcp_ok = mcp_calls(os.environ.get("DAASH_FAKE_MCP", ""), path)
    if not mcp_ok and os.environ.get("DAASH_FAKE_MCP_STRICT") == "1":
        text = "Could not use the DAASH tools. " + " ".join(notes)
        _emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": text,
               "usage": {"input_tokens": 0, "output_tokens": 0}, "total_cost_usd": 0})
        return 1
    full = os.path.join(os.getcwd(), path)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    body = make_content(path, prompt)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(body)
    summary = f"Wrote {path} ({len(body.split())} words) covering overview, details, steps and summary."
    if notes:
        summary += " " + " ".join(notes)
    _emit({"type": "assistant", "message": {"content": [{"type": "text", "text": summary}]}})
    _emit({"type": "result", "subtype": "success", "is_error": False, "result": summary,
           "usage": {"input_tokens": max(1, len(prompt) // 4), "output_tokens": len(body) // 4},
           "total_cost_usd": 0})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
