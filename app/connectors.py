"""Clients for connected agents (docs/DESIGN-0.4.md sections 5.3, 5.4 and 5.8): an A2A 1.0
client (OpenClaw's `a2a` channel and any other A2A agent), a Hermes Agent API server client, and
network discovery. httpx only; no database in here, so every rule is unit-testable against stub
servers (`transport=` takes an httpx transport, e.g. `httpx.MockTransport`).

Errors are `ConnectorError` with a `kind`:
* `unreachable` - no connection, a timeout, or no endpoint at that address;
* `token_rejected` - HTTP 401/403 (or an A2A auth error): the owner has not approved yet, or the
  token was changed;
* `rate_limited` - HTTP 429 or the A2A rate-limit error (OpenClaw: 30 requests a minute a peer);
* `server_error` - HTTP 5xx;
* `protocol` - an answer DAASH cannot use (not JSON, not JSON-RPC, a missing field);
* `not_found` - the task or run is unknown (an OpenClaw gateway forgets its tasks on restart);
* `refused` - the agent refused the request itself (not an infrastructure problem).
Every kind except `refused` is an infrastructure problem: it says nothing about the work.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

#: The ports discovery probes by default: OpenClaw's gateway and Hermes's API server.
DEFAULT_PORTS = (18789, 8642)
OPENCLAW_PORT = 18789
HERMES_PORT = 8642
CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")
DEFAULT_RPC_PATH = "/a2a/v1"
PROBE_TASK_ID = "daash-probe"
#: A2A message text DAASH sends at most (OpenClaw caps inbound text at 64 KiB).
MAX_MESSAGE_BYTES = 60_000

CARD_TIMEOUT = 6.0
CHECK_TIMEOUT = 8.0
RPC_TIMEOUT = 30.0
#: SendMessage asks to return immediately, but an agent that ignores that blocks until its reply
#: (OpenClaw's own limit is 120 s by default), so allow longer than that.
SEND_TIMEOUT = 150.0

INFRA_KINDS = ("unreachable", "token_rejected", "rate_limited", "server_error", "protocol",
               "not_found")
ERROR_TEXT = {
    "unreachable": "DAASH cannot reach the agent",
    "token_rejected": "the agent rejected DAASH's token",
    "rate_limited": "the agent is rate-limiting DAASH",
    "server_error": "the agent's server reported an error",
    "protocol": "the agent's answer could not be used",
    "not_found": "the agent does not know this task",
    "refused": "the agent refused the request",
}


class ConnectorError(Exception):
    def __init__(self, kind: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def infrastructure(self) -> bool:
        return self.kind in INFRA_KINDS

    def stored(self) -> str:
        """The form kept in `connected_agents.last_error`: '[kind] message'."""
        return f"[{self.kind}] {self}"


def split_stored_error(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """('kind', 'message') from a stored last_error ('[kind] message'); (None, text) for others."""
    if not text:
        return None, None
    m = re.match(r"^\[([a-z_]+)\] (.*)$", text, re.S)
    if m:
        return m.group(1), m.group(2)
    return None, text


def _client(transport: Optional[httpx.AsyncBaseTransport], timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False,
                             headers={"User-Agent": "DAASH/0.4"})


def _http_error(exc: Exception, where: str) -> ConnectorError:
    if isinstance(exc, httpx.TimeoutException):
        return ConnectorError("unreachable", f"{where} did not answer in time")
    return ConnectorError("unreachable", f"cannot connect to {where} ({type(exc).__name__})")


def _status_error(resp: httpx.Response, where: str) -> Optional[ConnectorError]:
    code = resp.status_code
    if code in (401, 403):
        return ConnectorError("token_rejected", f"{where} answered HTTP {code}: the token was "
                                                "not accepted", code)
    if code == 429:
        return ConnectorError("rate_limited", f"{where} answered HTTP 429 (too many requests)",
                              code)
    if code >= 500:
        return ConnectorError("server_error", f"{where} answered HTTP {code}", code)
    if code == 404:
        return ConnectorError("unreachable", f"{where} answered HTTP 404: nothing there", code)
    if code >= 400:
        return ConnectorError("protocol", f"{where} answered HTTP {code}", code)
    return None


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

@dataclass
class Address:
    """What a user typed: a URL, `host:port` or a bare host name."""
    scheme: str
    host: str
    port: Optional[int]
    path: str = ""

    @property
    def origin(self) -> str:
        if self.port is None:
            return f"{self.scheme}://{_host_for_url(self.host)}"
        return f"{self.scheme}://{_host_for_url(self.host)}:{self.port}"

    def with_port(self, port: int) -> "Address":
        return Address(self.scheme, self.host, port, self.path)


def _host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def parse_address(raw: str) -> Address:
    """`http://host:port/path`, `host:port`, `host` (bare: no port, try the default ports).
    Raises ValueError for something that is not an address."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("enter an address, for example jane-mac.tailnet.ts.net or "
                         "http://host:18789")
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"only http and https addresses are supported, not {parts.scheme!r}")
    if not parts.hostname:
        raise ValueError(f"{raw!r} has no host name")
    if parts.username or parts.password:
        raise ValueError("do not put credentials in the address")
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"{raw!r} has an invalid port") from None
    host = parts.hostname
    if not re.fullmatch(r"[A-Za-z0-9.\-_:]+", host):
        raise ValueError(f"{host!r} is not a valid host name")
    return Address(parts.scheme, host, port, parts.path.rstrip("/"))


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def card_urls(url: str) -> list[str]:
    """Where to look for the Agent Card of an entered URL: the URL itself when it names a card
    (a `.json` path), else the well-known paths on its origin."""
    addr = parse_address(url)
    if addr.path.endswith(".json"):
        return [addr.origin + addr.path]
    return [addr.origin + p for p in CARD_PATHS]


# ---------------------------------------------------------------------------
# Agent Cards
# ---------------------------------------------------------------------------

@dataclass
class CardInfo:
    name: str
    description: str = ""
    version: str = ""
    kind: str = "a2a"                   # openclaw | a2a
    rpc_url: str = ""                   # the advertised JSON-RPC URL (may be a LAN address)
    rpc_path: str = DEFAULT_RPC_PATH
    protocol_version: str = ""
    skills: list[dict[str, str]] = field(default_factory=list)


def _is_card(obj: Any) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("name"), str) and (
        "skills" in obj or "supportedInterfaces" in obj or "url" in obj
        or "capabilities" in obj or "protocolVersion" in obj)


def _rpc_interface(card: dict) -> tuple[str, str]:
    """(url, protocol version) of the card's JSON-RPC interface: A2A 1.0
    `supportedInterfaces[{url, protocolBinding: "JSONRPC"}]`, or the 0.x `url` with
    `preferredTransport`/`additionalInterfaces`."""
    for key in ("supportedInterfaces", "additionalInterfaces", "interfaces"):
        items = card.get(key)
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict) or not isinstance(it.get("url"), str):
                    continue
                binding = str(it.get("protocolBinding") or it.get("transport") or "JSONRPC")
                if binding.upper().replace("-", "") in ("JSONRPC", "JSONRPC2.0", "JSONRPC2"):
                    return it["url"], str(it.get("protocolVersion") or card.get(
                        "protocolVersion") or "")
    url = card.get("url")
    transport = str(card.get("preferredTransport") or "JSONRPC")
    if isinstance(url, str) and transport.upper().replace("-", "") in ("JSONRPC",):
        return url, str(card.get("protocolVersion") or "")
    return "", str(card.get("protocolVersion") or "")


def looks_like_openclaw(card: dict) -> bool:
    hay = " ".join(str(card.get(k) or "") for k in ("name", "description", "version"))
    prov = card.get("provider")
    if isinstance(prov, dict):
        hay += " " + " ".join(str(v) for v in prov.values())
    return "openclaw" in hay.lower()


def parse_card(card: Any) -> CardInfo:
    """The parts of an Agent Card DAASH uses. Raises ConnectorError(protocol) for a non-card."""
    if not _is_card(card):
        raise ConnectorError("protocol", "the answer is not an A2A Agent Card")
    url, version = _rpc_interface(card)
    path = urlsplit(url).path if url else ""
    skills = []
    for s in card.get("skills") or []:
        if isinstance(s, dict) and (s.get("id") or s.get("name")):
            skills.append({"id": str(s.get("id") or s.get("name")),
                           "name": str(s.get("name") or s.get("id"))})
    return CardInfo(name=card["name"].strip() or "A2A agent",
                    description=str(card.get("description") or "")[:500],
                    version=str(card.get("version") or ""),
                    kind="openclaw" if looks_like_openclaw(card) else "a2a",
                    rpc_url=url, rpc_path=path or DEFAULT_RPC_PATH, protocol_version=version,
                    skills=skills)


def endpoint_for(entered: str, info: CardInfo, override: Optional[str] = None) -> str:
    """The JSON-RPC endpoint DAASH uses (section 5.3): the card's JSON-RPC **path** on the
    **origin the user entered** (advertised hosts are often LAN addresses DAASH cannot reach);
    an explicit override wins."""
    if override and override.strip():
        return override.strip()
    return parse_address(entered).origin + (info.rpc_path or DEFAULT_RPC_PATH)


async def fetch_card(url: str, *, transport: Optional[httpx.AsyncBaseTransport] = None,
                     timeout: float = CARD_TIMEOUT) -> tuple[dict, str]:
    """(card JSON, the URL it came from). Never sends a credential."""
    last: Optional[ConnectorError] = None
    async with _client(transport, timeout) as client:
        for u in card_urls(url):
            try:
                resp = await client.get(u, headers={"Accept": "application/json"})
            except httpx.HTTPError as exc:
                raise _http_error(exc, origin_of(u)) from None
            if resp.status_code != 200:
                last = _status_error(resp, u) or ConnectorError("protocol", f"{u}: HTTP "
                                                                f"{resp.status_code}")
                continue
            try:
                card = resp.json()
            except ValueError:
                last = ConnectorError("protocol", f"{u} did not return JSON")
                continue
            if _is_card(card):
                return card, u
            last = ConnectorError("protocol", f"{u} is not an A2A Agent Card")
    raise last or ConnectorError("unreachable", f"no Agent Card at {url}")


# ---------------------------------------------------------------------------
# A2A 1.0 JSON-RPC client
# ---------------------------------------------------------------------------

TERMINAL_STATES = ("completed", "failed", "rejected", "canceled")
#: A2A's JSON-RPC error codes DAASH distinguishes.
TASK_NOT_FOUND = -32001
CANCEL_REFUSED = -32004   # OpenClaw: CancelTask is refused (task not cancelable)


def normalize_state(raw: Any) -> str:
    """'TASK_STATE_COMPLETED' / 'completed' -> 'completed'; unknown -> 'working'."""
    s = str(raw or "").strip().lower()
    if s.startswith("task_state_"):
        s = s[len("task_state_"):]
    s = s.replace("-", "_")
    if s == "cancelled":
        s = "canceled"
    return s or "working"


def parts_text(parts: Any) -> str:
    """The text of A2A message/artifact parts (1.0 `{text}` or 0.x `{kind: "text", text}`); JSON
    data parts are included compactly."""
    out = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        if isinstance(p.get("text"), str):
            out.append(p["text"])
        elif "data" in p:
            import json
            out.append(json.dumps(p["data"], separators=(",", ":"), default=str))
    return "\n".join(out)


@dataclass
class A2ATask:
    id: str
    state: str
    context_id: str = ""
    text: str = ""          # the reply: artifact text, else the status message's text
    raw: dict = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


def task_from_result(result: Any) -> A2ATask:
    """An A2ATask from a SendMessage/GetTask result: `{task: {...}}`, `{message: {...}}` (a
    direct reply, already complete) or a 0.x bare Task/Message."""
    if not isinstance(result, dict):
        raise ConnectorError("protocol", "the A2A result is not an object")
    if isinstance(result.get("task"), dict):
        result = result["task"]
    elif isinstance(result.get("message"), dict):
        msg = result["message"]
        return A2ATask(id=str(msg.get("taskId") or ""), state="completed",
                       context_id=str(msg.get("contextId") or ""),
                       text=parts_text(msg.get("parts")), raw=result)
    elif result.get("kind") == "message" or ("parts" in result and "status" not in result):
        return A2ATask(id=str(result.get("taskId") or ""), state="completed",
                       context_id=str(result.get("contextId") or ""),
                       text=parts_text(result.get("parts")), raw=result)
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    tid = result.get("id")
    if not isinstance(tid, str) or not tid:
        raise ConnectorError("protocol", "the A2A task has no id")
    state = normalize_state(status.get("state"))
    texts = [parts_text(a.get("parts")) for a in result.get("artifacts") or []
             if isinstance(a, dict)]
    text = "\n".join(t for t in texts if t)
    status_text = parts_text((status.get("message") or {}).get("parts")
                             if isinstance(status.get("message"), dict) else None)
    if not text or state in ("failed", "rejected"):
        text = text if not status_text else (status_text if not text else
                                             f"{text}\n{status_text}")
    return A2ATask(id=tid, state=state, context_id=str(result.get("contextId") or ""),
                   text=text, raw=result)


def classify_rpc_error(err: Any) -> ConnectorError:
    code = err.get("code") if isinstance(err, dict) else None
    message = str(err.get("message") if isinstance(err, dict) else err or "JSON-RPC error")
    low = message.lower()
    if code == TASK_NOT_FOUND or "not found" in low:
        return ConnectorError("not_found", f"A2A error {code}: {message}")
    if "rate" in low and "limit" in low or "too many" in low:
        return ConnectorError("rate_limited", f"A2A error {code}: {message}")
    if any(w in low for w in ("unauthorized", "unauthenticated", "forbidden", "invalid token",
                              "authentication")):
        return ConnectorError("token_rejected", f"A2A error {code}: {message}")
    return ConnectorError("refused", f"A2A error {code}: {message}")


def context_id(swarm_id: str, task_id: str, attempt: int) -> str:
    """`daash-<swarm8>-<task8>-a<attempt>`: one agent session per task attempt (section 5.3)."""
    def short(x: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", x)[:8] or "x"
    return f"daash-{short(swarm_id)}-{short(task_id)}-a{int(attempt)}"


class A2AClient:
    """JSON-RPC 2.0 over HTTP to one A2A endpoint with DAASH's peer token."""

    def __init__(self, endpoint: str, token: Optional[str], *,
                 transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.endpoint = endpoint
        self.token = token or ""
        self.transport = transport

    async def call(self, method: str, params: dict, timeout: float = RPC_TIMEOUT) -> Any:
        body = {"jsonrpc": "2.0", "id": f"daash-{uuid.uuid4().hex[:12]}", "method": method,
                "params": params}
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "A2A-Version": "1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        where = origin_of(self.endpoint)
        try:
            async with _client(self.transport, timeout) as client:
                resp = await client.post(self.endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise _http_error(exc, where) from None
        problem = _status_error(resp, where)
        if problem is not None:
            raise problem
        try:
            data = resp.json()
        except ValueError:
            raise ConnectorError("protocol", f"{where} did not answer with JSON") from None
        if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
            raise ConnectorError("protocol", f"{where} did not answer with JSON-RPC")
        if data.get("error") is not None:
            raise classify_rpc_error(data["error"])
        if "result" not in data:
            raise ConnectorError("protocol", f"{where} sent a JSON-RPC answer without a result")
        return data["result"]

    async def check(self) -> None:
        """The cheap check (spends no model tokens): GetTask for a task that does not exist. "Not
        found" (or any refusal that is not about auth) means reachable and the token accepted."""
        try:
            await self.call("GetTask", {"id": PROBE_TASK_ID}, timeout=CHECK_TIMEOUT)
        except ConnectorError as exc:
            if exc.kind in ("not_found", "refused"):
                return
            raise

    async def send(self, text: str, context: str, message_id: Optional[str] = None) -> A2ATask:
        params = {"message": {"messageId": message_id or f"daash-{uuid.uuid4()}",
                              "role": "ROLE_USER", "contextId": context,
                              "parts": [{"text": text}]},
                  "configuration": {"returnImmediately": True}}
        return task_from_result(await self.call("SendMessage", params, timeout=SEND_TIMEOUT))

    async def get(self, task_id: str) -> A2ATask:
        return task_from_result(await self.call("GetTask", {"id": task_id}))


# ---------------------------------------------------------------------------
# Hermes Agent API server
# ---------------------------------------------------------------------------

RUN_DONE = ("completed", "succeeded", "success", "done", "finished")
RUN_FAILED = ("failed", "error", "errored", "cancelled", "canceled", "stopped", "interrupted",
              "timeout", "timed_out", "expired")


_OC_VERSION_RE = re.compile(r'OPENCLAW_CONTROL_UI_BUILD_INFO\s*=\s*\{"version":"([^"]+)"')
#: An OpenClaw gateway's public liveness answer (verified on 2026.11.3; also /healthz).
OPENCLAW_HEALTH = {"ok": True, "status": "live"}


def openclaw_page_version(html_text: str) -> Optional[str]:
    """'' or the version when `html_text` (GET /) is OpenClaw's Control UI; None otherwise. The
    page carries `data-openclaw-control-ui-*` attributes and OPENCLAW_CONTROL_UI_BUILD_INFO."""
    head = (html_text or "")[:20000]
    if "data-openclaw-control-ui" not in head and "OPENCLAW_CONTROL_UI" not in head:
        return None
    m = _OC_VERSION_RE.search(head)
    return m.group(1) if m else ""


def looks_like_hermes_health(obj: Any) -> bool:
    """A Hermes API server's public /health answer (discovery never sends a credential)."""
    if not isinstance(obj, dict):
        return False
    text = " ".join(f"{k}={v}" for k, v in obj.items() if isinstance(v, (str, int, float)))
    return "hermes" in text.lower()


@dataclass
class HermesRun:
    id: str
    state: str              # running | completed | failed
    text: str = ""
    error: str = ""
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in ("completed", "failed")


def _run_text(obj: dict) -> str:
    for key in ("output", "final_output", "final_response", "response", "result", "text",
                "content", "message"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            inner = _run_text(v)
            if inner:
                return inner
        if isinstance(v, list):
            texts = []
            for item in v:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict):
                    t = _run_text(item)
                    if t:
                        texts.append(t)
            if texts:
                return "\n".join(texts)
    return ""


def run_from_json(obj: Any, run_id: str = "") -> HermesRun:
    if not isinstance(obj, dict):
        raise ConnectorError("protocol", "the Hermes run is not an object")
    rid = str(obj.get("run_id") or obj.get("id") or run_id or "")
    raw_state = str(obj.get("status") or obj.get("state") or "running").lower()
    if raw_state in RUN_DONE:
        state = "completed"
    elif raw_state in RUN_FAILED:
        state = "failed"
    else:
        state = "running"
    err = obj.get("error")
    error = (err.get("message") if isinstance(err, dict) else str(err or "")) or ""
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    return HermesRun(id=rid, state=state, text=_run_text(obj), error=str(error), usage=usage,
                     raw=obj)


class HermesClient:
    """Hermes Agent's API server (`API_SERVER_ENABLED`, port 8642, Bearer `API_SERVER_KEY`)."""

    def __init__(self, base_url: str, key: Optional[str], *,
                 transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.base = base_url.rstrip("/")
        if self.base.endswith("/v1"):
            self.base = self.base[:-3]
        self.key = key or ""
        self.transport = transport

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    async def _request(self, method: str, path: str, *, json: Any = None,
                       timeout: float = RPC_TIMEOUT, auth: bool = True,
                       headers: Optional[dict] = None) -> Any:
        where = self.base
        try:
            async with _client(self.transport, timeout) as client:
                resp = await client.request(method, self.base + path, json=json,
                                            headers={**(self._headers() if auth else {}),
                                                     **(headers or {})})
        except httpx.HTTPError as exc:
            raise _http_error(exc, where) from None
        if resp.status_code == 404 and path.startswith("/v1/runs/"):
            raise ConnectorError("not_found", f"{where} does not know run {path.split('/')[3]}",
                                 404)
        problem = _status_error(resp, where)
        if problem is not None:
            raise problem
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise ConnectorError("protocol", f"{where}{path} did not answer with JSON") from None

    async def health(self) -> dict:
        out = await self._request("GET", "/health", timeout=CHECK_TIMEOUT, auth=False)
        return out if isinstance(out, dict) else {}

    async def check(self) -> None:
        """The cheap check: the authenticated model list (no model tokens spent)."""
        await self._request("GET", "/v1/models", timeout=CHECK_TIMEOUT)

    async def start(self, text: str, session_id: str,
                    idempotency_key: Optional[str] = None) -> HermesRun:
        """POST /v1/runs (202 {run_id, status}). With an Idempotency-Key a resend returns the
        same run instead of starting the work twice."""
        body = {"input": text, "session_id": session_id}
        out = await self._request("POST", "/v1/runs", json=body, timeout=SEND_TIMEOUT,
                                  headers={"Idempotency-Key": idempotency_key}
                                  if idempotency_key else None)
        run = run_from_json(out)
        if not run.id:
            raise ConnectorError("protocol", "Hermes did not return a run id")
        return run

    async def get(self, run_id: str) -> HermesRun:
        return run_from_json(await self._request("GET", f"/v1/runs/{run_id}"), run_id)

    async def stop(self, run_id: str) -> None:
        await self._request("POST", f"/v1/runs/{run_id}/stop", json={})


# ---------------------------------------------------------------------------
# One interface for the engine
# ---------------------------------------------------------------------------

@dataclass
class Progress:
    """Where a connected agent's work stands."""
    external_id: str
    state: str              # working | completed | failed
    text: str = ""


async def check_agent(agent: Any, *, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
    """The cheap check of a direct connected agent; raises ConnectorError."""
    if agent.protocol == "a2a":
        await A2AClient(agent.endpoint or "", agent.token, transport=transport).check()
    elif agent.protocol == "hermes-api":
        await HermesClient(agent.url or "", agent.token, transport=transport).check()
    else:
        raise ConnectorError("protocol", f"DAASH cannot check a {agent.protocol!r} agent itself")


async def start_work(agent: Any, text: str, session: str, *, message_id: Optional[str] = None,
                     transport: Optional[httpx.AsyncBaseTransport] = None) -> Progress:
    """Send the work. `message_id` should be stable per job: OpenClaw answers a resend of the
    same messageId and contextId with the same task, so a retry never starts the work twice."""
    if agent.protocol == "a2a":
        t = await A2AClient(agent.endpoint or "", agent.token, transport=transport).send(
            text, session, message_id)
        return Progress(t.id, _a2a_progress_state(t.state), t.text)
    if agent.protocol == "hermes-api":
        r = await HermesClient(agent.url or "", agent.token, transport=transport).start(
            text, session, message_id)
        return Progress(r.id, _run_progress_state(r.state), r.text or r.error)
    raise ConnectorError("protocol", f"cannot send work to a {agent.protocol!r} agent directly")


async def poll_work(agent: Any, external_id: str, *,
                    transport: Optional[httpx.AsyncBaseTransport] = None) -> Progress:
    if agent.protocol == "a2a":
        t = await A2AClient(agent.endpoint or "", agent.token, transport=transport).get(
            external_id)
        return Progress(t.id or external_id, _a2a_progress_state(t.state), t.text)
    r = await HermesClient(agent.url or "", agent.token, transport=transport).get(external_id)
    return Progress(r.id or external_id, _run_progress_state(r.state), r.text or r.error)


async def cancel_work(agent: Any, external_id: str, *,
                      transport: Optional[httpx.AsyncBaseTransport] = None) -> bool:
    """Hermes: stop the run. A2A (OpenClaw) refuses CancelTask, so nothing is sent: the MCP
    server refuses the job's calls instead and a late reply is ignored."""
    if agent.protocol == "hermes-api" and external_id:
        await HermesClient(agent.url or "", agent.token, transport=transport).stop(external_id)
        return True
    return False


def _a2a_progress_state(state: str) -> str:
    if state == "completed":
        return "completed"
    if state in ("failed", "rejected", "canceled"):
        return "failed"
    if state in ("input_required", "auth_required"):
        return "failed"     # DAASH cannot answer the agent's question: the attempt ends
    return "working"


def _run_progress_state(state: str) -> str:
    return {"completed": "completed", "failed": "failed"}.get(state, "working")


# ---------------------------------------------------------------------------
# Discovery (section 5.8)
# ---------------------------------------------------------------------------

CONNECT_TIMEOUT = 1.5
HTTP_TIMEOUT = 3.0
DISCOVERY_CONCURRENCY = 128
MAX_SUBNET_PREFIX = 24
SILENT_DETAIL = ("Something is listening but does not answer (probably a firewall); connect it "
                 "through an agent host instead.")


@dataclass
class Finding:
    host: str
    port: int
    kind: str               # openclaw | a2a | hermes
    status: str             # found | silent
    name: Optional[str] = None
    url: Optional[str] = None
    card: Optional[dict] = None
    detail: Optional[str] = None


def parse_list(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,\s]+", raw or "") if x.strip()]


def is_container_id(host: str) -> bool:
    """A Docker container's default host name (12 or 64 hex characters). A runner inside a container
    reports it as its host, but it names no machine that discovery could look at."""
    return bool(re.fullmatch(r"[0-9a-f]{12}|[0-9a-f]{64}", (host or "").strip().lower()))


def parse_ports(raw: str) -> list[int]:
    out: list[int] = []
    for x in parse_list(raw):
        try:
            p = int(x)
        except ValueError:
            continue
        if 0 < p < 65536 and p not in out:
            out.append(p)
    return out or list(DEFAULT_PORTS)


def expand_subnets(raw: str) -> tuple[list[str], list[str]]:
    """(addresses, problems) of DAASH_DISCOVERY_SUBNETS: private IPv4 CIDRs no larger than /24."""
    hosts: list[str] = []
    problems: list[str] = []
    for cidr in parse_list(raw):
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            problems.append(f"{cidr!r} is not a CIDR such as 10.0.0.0/24")
            continue
        if net.version != 4:
            problems.append(f"{cidr}: only IPv4 subnets are scanned")
            continue
        if net.prefixlen < MAX_SUBNET_PREFIX:
            problems.append(f"{cidr} is larger than /{MAX_SUBNET_PREFIX}; split it")
            continue
        if not net.is_private:
            problems.append(f"{cidr} is not a private network; DAASH scans private ones only")
            continue
        hosts.extend(str(h) for h in (net.hosts() if net.num_addresses > 2 else net))
    return list(dict.fromkeys(hosts)), problems


def targets(hosts: Iterable[str], ports: Iterable[int]) -> list[tuple[str, int]]:
    """(host, port) pairs; a host given as `host:port` is probed on that port only."""
    out: list[tuple[str, int]] = []
    for h in hosts:
        h = h.strip()
        if not h:
            continue
        try:
            addr = parse_address(h)
        except ValueError:
            continue
        for p in ([addr.port] if addr.port else ports):
            if (addr.host, p) not in out:
                out.append((addr.host, p))
    return out


async def _tcp_open(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 1.0)
    except Exception:  # noqa: BLE001
        pass
    return True


async def probe(host: str, port: int, *, transport: Optional[httpx.AsyncBaseTransport] = None,
                tcp_open: Any = None, connect_timeout: float = CONNECT_TIMEOUT,
                http_timeout: float = HTTP_TIMEOUT, scheme: str = "http") -> Optional[Finding]:
    """What listens on host:port, without sending any credential: an A2A Agent Card, a Hermes
    API server (/health), 'silent' (TCP accepts, HTTP never answers), or None (nothing, or
    something else)."""
    opener = tcp_open or _tcp_open
    if transport is None and not await opener(host, port, connect_timeout):
        return None
    origin = f"{scheme}://{_host_for_url(host)}:{port}"
    async with _client(transport, http_timeout) as client:
        answered = False
        for path in CARD_PATHS[:1]:
            try:
                resp = await client.get(origin + path, headers={"Accept": "application/json"})
            except httpx.TimeoutException:
                return Finding(host, port, "unknown", "silent", detail=SILENT_DETAIL)
            except httpx.HTTPError:
                return None
            answered = True
            if resp.status_code == 200:
                try:
                    card = resp.json()
                except ValueError:
                    card = None
                if _is_card(card):
                    info = parse_card(card)
                    return Finding(host, port, info.kind, "found", name=info.name, url=origin,
                                   card=card, detail=_card_detail(info))
        try:
            resp = await client.get(origin + "/health", headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            return None if answered else Finding(host, port, "unknown", "silent",
                                                 detail=SILENT_DETAIL)
        except httpx.HTTPError:
            return None
        health = None
        if resp.status_code == 200:
            try:
                health = resp.json()
            except ValueError:
                health = None
            if looks_like_hermes_health(health):
                return Finding(host, port, "hermes", "found", name=f"Hermes on {host}",
                               url=origin, card={"health": health},
                               detail="Hermes Agent API server")
        # An OpenClaw gateway whose owner has not turned A2A on yet serves no card, but its
        # Control UI page and /health say what it is (no credential needed).
        version: Optional[str] = None
        try:
            page = await client.get(origin + "/", headers={"Accept": "text/html"})
            if page.status_code == 200:
                version = openclaw_page_version(page.text)
        except httpx.HTTPError:
            version = None
        if version is None and health == OPENCLAW_HEALTH and port == OPENCLAW_PORT:
            version = ""
        if version is not None:
            return Finding(host, port, "openclaw", "found", name=f"OpenClaw on {host}",
                           url=origin, card=None,
                           detail=f"OpenClaw{(' ' + version) if version else ''} gateway; its "
                                  "A2A channel is off: Connect gives its owner the one step "
                                  "that turns it on")
    return None


def _card_detail(info: CardInfo) -> str:
    who = "OpenClaw" if info.kind == "openclaw" else "A2A agent"
    skills = ", ".join(s["name"] for s in info.skills[:6])
    s = f"{who}{(' ' + info.version) if info.version else ''}"
    return s + (f"; agents: {skills}" if skills else "")


async def discover(pairs: Iterable[tuple[str, int]], *, concurrency: int = DISCOVERY_CONCURRENCY,
                   transport: Optional[httpx.AsyncBaseTransport] = None, tcp_open: Any = None,
                   connect_timeout: float = CONNECT_TIMEOUT,
                   http_timeout: float = HTTP_TIMEOUT) -> list[Finding]:
    """Probe every (host, port) concurrently; what was found or is silent."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(h: str, p: int) -> Optional[Finding]:
        async with sem:
            try:
                return await probe(h, p, transport=transport, tcp_open=tcp_open,
                                   connect_timeout=connect_timeout, http_timeout=http_timeout)
            except Exception:  # noqa: BLE001 - one odd host never spoils the look
                return None

    found = await asyncio.gather(*(one(h, p) for h, p in pairs))
    return [f for f in found if f is not None]
