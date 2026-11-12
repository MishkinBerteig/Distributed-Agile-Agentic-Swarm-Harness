"""Harness adapters: build argv/env/config for each agent CLI and parse its output.

Flags were verified against OpenClaude 0.31.0, Claude Code 2.1.319, codex-cli 0.160.0, OpenClaw
2026.11.3 and Hermes Agent 0.23.1 running against the DAASH LLM gateway (docs/RUNNER.md). Every
adapter points its CLI at runner-private config/state folders and scrubs inherited credentials, so
a user's own Claude Code / OpenAI / Anthropic / OpenClaw / Hermes login is never used.

DAASH tools (docs/DESIGN-0.4.md section 4.2): when the lease carries an MCP endpoint, each CLI
gets the `daash` MCP server for its job. The job token is passed in the environment
(`DAASH_MCP_TOKEN`); the per-job config files refer to it instead of containing it.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping

# Credentials/endpoints from the operator's shell that must never leak into a job.
SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CONFIG_DIR", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "OPENAI_API_BASE", "OPENAI_ORG_ID", "OPENAI_MODEL", "CODEX_API_KEY", "CODEX_HOME",
    "OPENCLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_OPENAI", "CLAUDE_CODE_USE_GEMINI",
    "GEMINI_API_KEY", "GOOGLE_API_KEY",
    # OpenClaw / Hermes homes, profiles and keys of the operator's own installs
    "OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "OPENCLAW_PROFILE",
    "OPENCLAW_AGENT_DIR", "OPENCLAW_WORKSPACE_DIR", "OPENCLAW_GATEWAY_TOKEN", "OPENCLAW_GATEWAY_PASSWORD",
    "OPENCLAW_GATEWAY_URL", "HERMES_HOME", "HERMES_PROFILE", "OPENROUTER_API_KEY", "NOUS_API_KEY",
    # a job token must come from this job's lease only
    "DAASH_MCP_URL", "DAASH_MCP_TOKEN",
)

_INFRA = re.compile(
    r"(api error|http|status|code|error)[:= ]*\s*\"?(401|403|429|5\d\d)\b"
    r"|authentication[_ ]failed|unauthori[sz]ed|invalid[_ ]api[_ ]key|invalid_request_error"
    r"|invalid or missing api key|no api key|couldn'?t sign in|couldn'?t connect"
    r"|ECONNREFUSED|ENOTFOUND|ETIMEDOUT|EAI_AGAIN|ECONNRESET|EHOSTUNREACH|connection (refused|reset|error|closed)"
    r"|unreachable|unable to connect|could not resolve|fetch failed|socket hang up|overloaded"
    r"|service unavailable|bad gateway|gateway timeout|command not found|not found on PATH"
    r"|model gateway|rate limit",
    re.I,
)

MCP_SERVER = "daash"
MCP_TOKEN_ENV = "DAASH_MCP_TOKEN"
MCP_URL_ENV = "DAASH_MCP_URL"


def looks_like_infra_error(text: str) -> bool:
    return bool(text and _INFRA.search(text))


@dataclass
class JobSpec:
    model: str
    system_prompt: str
    prompt: str
    state_dir: str                 # runner-private folder for this harness (config/history)
    artifact: str = ""
    job_dir: str = ""              # runner-private folder of this job, outside its workspace
    workdir: str = ""              # the job's workspace (the CLI's cwd)
    timeout_seconds: int = 0       # the runner's limit for this job (0 = unknown)
    mcp_url: str = ""              # DAASH MCP endpoint for this job ("" = no DAASH tools)
    mcp_token: str = ""            # the job's bearer token for it

    @property
    def private_dir(self) -> str:
        """Per-job folder for generated CLI config (never inside the workspace)."""
        return os.path.join(self.job_dir or self.state_dir, "cli")


@dataclass
class Invocation:
    argv: list[str]
    env: dict[str, str]
    stdin: str | None = None
    files: dict[str, str] = field(default_factory=dict)      # written (0600) before the CLI starts
    dirs: list[str] = field(default_factory=list)            # created (0700) before the CLI starts
    cleanup: list[list[str]] = field(default_factory=list)   # run with `env` after the CLI exits
    remove_after: list[str] = field(default_factory=list)    # deleted after the cleanup commands


@dataclass
class Parsed:
    final_text: str = ""
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    cost_usd: float | None = None
    outcome: str = "failed"        # submitted | failed | error
    error: str = ""


def _usage(prompt: int, completion: int) -> dict:
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def base_env(hcfg: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Inherited environment without credentials, overlaid with the harness config env."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k not in SCRUBBED_ENV}
    env.update(hcfg.get("env") or {})
    return env


def _mcp_env(env: dict[str, str], spec: JobSpec) -> bool:
    """Put the job's MCP token in the CLI environment. False when the job has no DAASH tools."""
    if not (spec.mcp_url and spec.mcp_token):
        return False
    env[MCP_TOKEN_ENV] = spec.mcp_token
    return True


def _jsonl(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            events.append(d)
    return events


def _no_result(stdout: str, stderr: str, exit_code: int | None, what: str) -> Parsed:
    detail = (stderr.strip() or stdout.strip())[-600:]
    p = Parsed(outcome="failed", error=f"{what} produced no result (exit code {exit_code})" + (f": {detail}" if detail else ""))
    if exit_code == 127 or looks_like_infra_error(detail):
        p.outcome = "error"
    return p


def _gateway_url(env: Mapping[str, str], hcfg: Mapping[str, Any]) -> str:
    return (hcfg.get("base_url") or env.get("DAASH_GATEWAY_URL") or "").rstrip("/")


def _model_list(hcfg: Mapping[str, Any], model: str) -> list[str]:
    return list(dict.fromkeys([m for m in [model, *(hcfg.get("models") or [])] if m]))


# --------------------------------------------------------------------------- Claude-Code-style

def _claude_mcp(spec: JobSpec, env: dict[str, str], files: dict[str, str]) -> list[str]:
    """`--mcp-config` file with the daash HTTP server; its tools are pre-approved so a headless
    run can call them whatever the permission mode."""
    if not _mcp_env(env, spec):
        return []
    path = os.path.join(spec.private_dir, "mcp.json")
    files[path] = json.dumps({"mcpServers": {MCP_SERVER: {
        "type": "http", "url": spec.mcp_url,
        "headers": {"Authorization": "Bearer ${%s}" % MCP_TOKEN_ENV}}}}, indent=2)
    return ["--mcp-config", path, "--strict-mcp-config", "--allowedTools", f"mcp__{MCP_SERVER}"]


def _build_claude_like(hcfg, spec: JobSpec, env: dict[str, str], default_mode: str) -> Invocation:
    files: dict[str, str] = {}
    argv = [hcfg["command"], "-p", "--output-format", "stream-json", "--verbose"]
    # Variadic options (--mcp-config, --allowedTools) come first: a flag always follows them.
    argv += _claude_mcp(spec, env, files)
    argv += ["--model", spec.model,
             "--permission-mode", hcfg.get("permission_mode") or default_mode]
    if spec.system_prompt:
        argv += ["--append-system-prompt", spec.system_prompt]
    argv += list(hcfg.get("args") or [])
    # The prompt goes on stdin (no argv length limit).
    return Invocation(argv=argv, env=env, stdin=spec.prompt, files=files)


def build_claude_code(hcfg, spec: JobSpec, environ=None) -> Invocation:
    env = base_env(hcfg, environ)
    small = hcfg.get("small_model") or "instant"
    env.update({
        "CLAUDE_CONFIG_DIR": spec.state_dir,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": small,
        "ANTHROPIC_SMALL_FAST_MODEL": small,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": spec.model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": spec.model,
        "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_MAX_RETRIES": "3",
    })
    env.update({k: v for k, v in (hcfg.get("env") or {}).items() if k.startswith("ANTHROPIC_")})
    return _build_claude_like(hcfg, spec, env, "bypassPermissions")


def build_openclaude(hcfg, spec: JobSpec, environ=None) -> Invocation:
    env = base_env(hcfg, environ)
    env.update({
        "CLAUDE_CONFIG_DIR": spec.state_dir, "OPENCLAUDE_CONFIG_DIR": spec.state_dir,
        "CLAUDE_CODE_USE_OPENAI": "1",
        "DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1",
    })
    env.update({k: v for k, v in (hcfg.get("env") or {}).items() if k.startswith(("OPENAI_", "CLAUDE_CODE_USE_"))})
    return _build_claude_like(hcfg, spec, env, "fullAccess")


def parse_claude_like(stdout: str, stderr: str, exit_code: int | None, what: str = "the CLI") -> Parsed:
    events = _jsonl(stdout)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None:
        return _no_result(stdout, stderr, exit_code, what)
    u = result.get("usage") or {}
    prompt = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
        + int(u.get("cache_creation_input_tokens") or 0)
    p = Parsed(final_text=str(result.get("result") or ""), usage=_usage(prompt, int(u.get("output_tokens") or 0)))
    cost = result.get("total_cost_usd")
    p.cost_usd = float(cost) if isinstance(cost, (int, float)) else None
    if result.get("is_error"):
        text = p.final_text or str(result.get("subtype") or "error")
        p.error = text[:1000]
        status = result.get("api_error_status")
        p.outcome = "error" if (isinstance(status, int) and (status in (401, 403, 429) or status >= 500)
                                or looks_like_infra_error(text)) else "failed"
        return p
    if exit_code not in (0, None):
        p.outcome, p.error = "failed", f"{what} exited with code {exit_code}"
    elif not p.final_text.strip():
        p.outcome, p.error = "failed", "the agent ended without a final answer"
    else:
        p.outcome = "submitted"
    return p


# --------------------------------------------------------------------------- Codex

def build_codex(hcfg, spec: JobSpec, environ=None) -> Invocation:
    env = base_env(hcfg, environ)
    env["CODEX_HOME"] = spec.state_dir
    base_url = env.get("DAASH_GATEWAY_URL", "")
    key_var = hcfg.get("api_key_env") or "DAASH_LLM_API_KEY"
    sandbox = hcfg.get("sandbox") or "workspace-write"
    argv = [hcfg["command"], "exec", "--json", "--skip-git-repo-check"]
    if sandbox == "bypass":
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv += ["--sandbox", sandbox]
    argv += ["-m", spec.model,
             "-c", 'model_provider="daash"',
             "-c", "model_providers.daash={name=%s, base_url=%s, env_key=%s, wire_api=\"responses\"}"
                   % (json.dumps("daash"), json.dumps(base_url), json.dumps(key_var)),
             # The gateway implements function tools only, not the hosted web_search tool.
             "-c", 'web_search="disabled"']
    if _mcp_env(env, spec):
        # Streamable HTTP server; Codex sends `Authorization: Bearer $DAASH_MCP_TOKEN`. Its tools
        # are approved up front: `codex exec` cannot ask anyone.
        argv += ["-c", f"mcp_servers.{MCP_SERVER}.url={json.dumps(spec.mcp_url)}",
                 "-c", f"mcp_servers.{MCP_SERVER}.bearer_token_env_var={json.dumps(MCP_TOKEN_ENV)}",
                 "-c", f'mcp_servers.{MCP_SERVER}.default_tools_approval_mode="approve"']
    if spec.system_prompt:
        argv += ["-c", "developer_instructions=" + json.dumps(spec.system_prompt)]
    argv += list(hcfg.get("args") or [])
    argv.append("-")        # prompt from stdin
    return Invocation(argv=argv, env=env, stdin=spec.prompt)


def parse_codex(stdout: str, stderr: str, exit_code: int | None) -> Parsed:
    events = _jsonl(stdout)
    if not any(e.get("type", "").startswith(("turn.", "thread.", "item.")) for e in events):
        return _no_result(stdout, stderr, exit_code, "codex")
    prompt = completion = 0
    final = ""
    failure = ""
    completed = False
    for e in events:
        t = e.get("type")
        if t == "turn.completed":
            completed = True
            u = e.get("usage") or {}
            prompt += int(u.get("input_tokens") or 0)
            completion += int(u.get("output_tokens") or 0)
        elif t == "turn.failed":
            failure = str((e.get("error") or {}).get("message") or "turn failed")
        elif t == "error" and not failure:
            failure = str(e.get("message") or "error")
        elif t == "item.completed" and (e.get("item") or {}).get("type") == "agent_message":
            final = str(e["item"].get("text") or "")
    p = Parsed(final_text=final, usage=_usage(prompt, completion))
    if failure:
        p.error = failure[:1000]
        p.outcome = "error" if looks_like_infra_error(failure) else "failed"
    elif exit_code not in (0, None):
        p.outcome, p.error = "failed", f"codex exited with code {exit_code}"
    elif not completed or not final.strip():
        p.outcome, p.error = "failed", "the agent ended without a final answer"
    else:
        p.outcome = "submitted"
    return p


# --------------------------------------------------------------------------- OpenClaw

#: Seconds `openclaw agent exec` gets less than the runner's job timeout, so it can stop on its own
#: (exit 2 with a JSON envelope) before the runner kills it.
OPENCLAW_TIMEOUT_MARGIN = 30


def openclaw_config(hcfg: Mapping[str, Any], spec: JobSpec, base_url: str) -> dict:
    """The per-job OpenClaw config (`--config`): only the DAASH gateway as a provider, plus the
    daash MCP server. `${VAR}` values are substituted by OpenClaw from the CLI environment."""
    key_var = hcfg.get("api_key_env") or "DAASH_LLM_API_KEY"
    models = [{
        "id": m, "name": m,
        "contextWindow": int(hcfg.get("context_window") or 131072),
        "maxTokens": int(hcfg.get("max_tokens") or 16384),
        # the gateway reports usage in streams only when asked
        "compat": {"supportsUsageInStreaming": True},
    } for m in _model_list(hcfg, spec.model)]
    cfg: dict[str, Any] = {
        "models": {"mode": "replace", "providers": {"daash": {
            "baseUrl": base_url, "api": hcfg.get("api") or "openai-completions",
            "apiKey": "${%s}" % key_var,
            "timeoutSeconds": max(60, spec.timeout_seconds or 1800),
            "models": models}}},
        "agents": {"defaults": {"model": {"primary": f"daash/{spec.model}", "fallbacks": []}}},
    }
    if spec.mcp_url and spec.mcp_token:
        cfg["mcp"] = {"servers": {MCP_SERVER: {
            "url": spec.mcp_url, "transport": "streamable-http",
            "headers": {"Authorization": "Bearer ${%s}" % MCP_TOKEN_ENV}}}}
    return cfg


def build_openclaw(hcfg, spec: JobSpec, environ=None) -> Invocation:
    env = base_env(hcfg, environ)
    _mcp_env(env, spec)
    root = os.path.join(spec.private_dir, "openclaw")
    home, state, cfg_path = os.path.join(root, "home"), os.path.join(root, "state"), os.path.join(root, "openclaw.json")
    # Everything OpenClaw resolves from a home (state, config, plugins, credentials) is per job.
    env.update({"OPENCLAW_HOME": home, "OPENCLAW_STATE_DIR": state, "OPENCLAW_CONFIG_PATH": cfg_path,
                "OPENCLAW_NO_AUTO_UPDATE": "1"})
    cfg = openclaw_config(hcfg, spec, _gateway_url(env, hcfg))
    timeout = max(60, (spec.timeout_seconds or 1800) - OPENCLAW_TIMEOUT_MARGIN)
    argv = [hcfg["command"], "agent", "exec", "--config", cfg_path, "--state-dir", state,
            "--message-file", "-", "--cwd", spec.workdir or ".", "--json",
            "--model", f"daash/{spec.model}", "--timeout", str(timeout)]
    if hcfg.get("thinking"):
        argv += ["--thinking", str(hcfg["thinking"])]
    argv += list(hcfg.get("args") or [])
    # `agent exec` has no system-prompt option: DAASH's system prompt leads the message.
    message = f"{spec.system_prompt}\n\n---\n\n{spec.prompt}" if spec.system_prompt else spec.prompt
    return Invocation(argv=argv, env=env, stdin=message, files={cfg_path: json.dumps(cfg, indent=2)},
                      dirs=[home, state])


def _json_envelope(stdout: str) -> dict | None:
    """The last top-level JSON object in stdout (OpenClaw pretty-prints its envelope)."""
    text = stdout.strip()
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except ValueError:
        pass
    dec = json.JSONDecoder()
    found = None
    for m in re.finditer(r"(?m)^\{", text):
        try:
            d, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(d, dict) and ("status" in d or "ok" in d):
            found = d
    return found


def _openclaw_usage(u: Any) -> dict:
    """`usage` is `{input, output, total, cacheRead?}` when the provider reported it; older or
    usage-less runs give `{usageReport: {state: "unavailable"}, ...}`."""
    if not isinstance(u, dict):
        return _usage(0, 0)
    src = u if "input" in u or "output" in u else (u.get("usageReport") or {})
    if not isinstance(src, dict):
        return _usage(0, 0)
    prompt = _int(src.get("input")) + _int(src.get("cacheRead")) + _int(src.get("cacheWrite"))
    return _usage(prompt, _int(src.get("output")))


def parse_openclaw(stdout: str, stderr: str, exit_code: int | None) -> Parsed:
    env = _json_envelope(stdout)
    if env is None:
        p = _no_result(stdout, stderr, exit_code, "openclaw")
        if exit_code == 2 and p.outcome != "error":
            p.error = "openclaw timed out"
        return p
    final = str(env.get("final") or "").strip() or "\n".join(
        str(x.get("text") or "") for x in env.get("payloads") or []
        if isinstance(x, dict) and not x.get("isError")).strip()
    p = Parsed(final_text=final, usage=_openclaw_usage(env.get("usage")))
    cost = env.get("costUsd")
    p.cost_usd = float(cost) if isinstance(cost, (int, float)) else None
    status = str(env.get("status") or "")
    err = env.get("error") if isinstance(env.get("error"), dict) else {}
    message = str(err.get("message") or "")
    if exit_code == 2 or status == "timeout":
        # An unreachable gateway also ends as a timeout ("Couldn't connect to the AI service").
        p.outcome = "error" if looks_like_infra_error(message) else "failed"
        p.error = ("openclaw timed out" + (f": {message}" if message else ""))[:1000]
    elif err.get("kind") == "cleanup" and final:
        p.outcome = "submitted"     # the work finished; only OpenClaw's own temp cleanup failed
    elif status == "error" or env.get("ok") is False:
        p.error = (message or "openclaw reported an error")[:1000]
        # model/provider/network/auth problems are infrastructure (DESIGN-0.4 4.3)
        hint = message + "\n" + stderr[-3000:]
        p.outcome = "error" if (env.get("model") is None and env.get("provider") is None
                                or looks_like_infra_error(hint)
                                or re.search(r"reason=(auth|network|rate|timeout|overloaded|billing)", hint)) \
            else "failed"
    elif exit_code not in (0, None):
        p.outcome, p.error = "failed", f"openclaw exited with code {exit_code}"
    elif not final:
        p.outcome, p.error = "failed", "the agent ended without a final answer"
    else:
        p.outcome = "submitted"
    return p


# --------------------------------------------------------------------------- Hermes Agent

#: Hermes toolsets a DAASH job never gets: DAASH memory is the shared one (MCP `remember`/`recall`),
#: and a job must not leave skills or cron jobs behind.
HERMES_DISABLED_TOOLSETS = ("memory", "session_search", "skills", "cronjob")


def hermes_config(hcfg: Mapping[str, Any], spec: JobSpec, base_url: str) -> dict:
    """The job's `$HERMES_HOME/config.yaml` (written as JSON, which YAML reads as is)."""
    key_var = hcfg.get("api_key_env") or "DAASH_LLM_API_KEY"
    cfg: dict[str, Any] = {
        "model": {"default": spec.model, "provider": "custom", "base_url": base_url,
                  "key_env": key_var, "context_length": int(hcfg.get("context_length") or 131072)},
        # the runner container is the sandbox; commands run in the job's workspace
        "terminal": {"backend": "local", "cwd": "."},
        "memory": {"memory_enabled": False, "user_profile_enabled": False, "nudge_interval": 0},
        "skills": {"creation_nudge_interval": 0},
        "agent": {"max_turns": int(hcfg.get("max_turns") or 90),
                  "disabled_toolsets": list(HERMES_DISABLED_TOOLSETS)},
        "auth": {"adopt_external_logins": False},     # never borrow a Codex/Claude Code login
        "updates": {"check": False},
        "telemetry": {"shared_metrics": {"enabled": False, "send": False}},
    }
    if spec.system_prompt:
        cfg["agent"]["system_prompt"] = spec.system_prompt
    if spec.mcp_url and spec.mcp_token:
        cfg["mcp_servers"] = {MCP_SERVER: {
            "url": spec.mcp_url, "headers": {"Authorization": "Bearer ${%s}" % MCP_TOKEN_ENV},
            "timeout": 120, "connect_timeout": 20}}
    return cfg


def build_hermes(hcfg, spec: JobSpec, environ=None) -> Invocation:
    env = base_env(hcfg, environ)
    _mcp_env(env, spec)
    home = os.path.join(spec.private_dir, "hermes-home")
    env["HERMES_HOME"] = home
    cfg = hermes_config(hcfg, spec, _gateway_url(env, hcfg))
    argv = [hcfg["command"], "chat", "--query-file", "-", "--format", "stream-json", "--yolo",
            "--in", spec.workdir or ".", "-m", spec.model,
            "--max-turns", str(int(hcfg.get("max_turns") or 90)), "--source", "tool"]
    argv += list(hcfg.get("args") or [])
    return Invocation(
        argv=argv, env=env, stdin=spec.prompt,
        files={os.path.join(home, "config.yaml"): json.dumps(cfg, indent=2, ensure_ascii=False)},
        dirs=[home],
        # Every Hermes CLI attaches to a gateway of its HERMES_HOME, which outlives the CLI.
        cleanup=[[hcfg["command"], "gateway", "stop"]],
        # The job's copy of Hermes's dependency environment (hundreds of MB) is not needed afterwards.
        remove_after=[os.path.join(home, "installs"), os.path.join(home, "cache")])


def parse_hermes(stdout: str, stderr: str, exit_code: int | None) -> Parsed:
    events = _jsonl(stdout)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None:
        p = _no_result(stdout, stderr, exit_code, "hermes")
        if exit_code == 3:
            p.outcome = "error"
            p.error = "hermes lost its gateway (exit code 3): " + p.error
        return p
    text = str(result.get("text") or "").strip() or "".join(
        str(e.get("text") or "") for e in events if e.get("type") == "text").strip()
    t = result.get("tokens") or {}
    p = Parsed(final_text=text, usage=_usage(_int(t.get("input")), _int(t.get("output"))))
    cost = result.get("cost_usd")
    p.cost_usd = float(cost) if isinstance(cost, (int, float)) else None
    code = result.get("exit_code", exit_code)
    error = str(result.get("error") or "")
    if exit_code == 3 or code == 3:
        p.outcome, p.error = "error", ("hermes lost its gateway (exit code 3)" + (f": {error}" if error else ""))[:1000]
    elif error:
        p.error = error[:1000]
        p.outcome = "error" if looks_like_infra_error(error + "\n" + stderr[-2000:]) else "failed"
    elif exit_code not in (0, None) or code not in (0, None):
        p.outcome, p.error = "failed", f"hermes exited with code {exit_code if exit_code not in (0, None) else code}"
    elif not text:
        p.outcome, p.error = "failed", "the agent ended without a final answer"
    else:
        p.outcome = "submitted"
    return p


# --------------------------------------------------------------------------- fake

def build_fake(hcfg, spec: JobSpec, environ=None) -> Invocation:
    env = base_env(hcfg, environ)
    if spec.artifact and "DAASH_FAKE_ARTIFACT" not in env:
        env["DAASH_FAKE_ARTIFACT"] = spec.artifact
    if _mcp_env(env, spec):
        env[MCP_URL_ENV] = spec.mcp_url
    py = hcfg.get("command") or sys.executable
    argv = [py, "-m", "daash_runner.fake_cli", "--model", spec.model] + list(hcfg.get("args") or [])
    # PYTHONPATH so the module resolves from the job's cwd.
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return Invocation(argv=argv, env=env, stdin=spec.prompt)


ADAPTERS = {
    "openclaude": (build_openclaude, lambda o, e, c: parse_claude_like(o, e, c, "openclaude")),
    "claude-code": (build_claude_code, lambda o, e, c: parse_claude_like(o, e, c, "claude")),
    "codex": (build_codex, parse_codex),
    "openclaw": (build_openclaw, parse_openclaw),
    "hermes": (build_hermes, parse_hermes),
    "fake": (build_fake, lambda o, e, c: parse_claude_like(o, e, c, "the fake CLI")),
}


def build(harness: str, hcfg: Mapping[str, Any], spec: JobSpec, environ=None) -> Invocation:
    return ADAPTERS[harness][0](hcfg, spec, environ)


def parse(harness: str, stdout: str, stderr: str, exit_code: int | None) -> Parsed:
    return ADAPTERS[harness][1](stdout, stderr, exit_code)
