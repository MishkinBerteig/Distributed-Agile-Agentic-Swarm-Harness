"""Harness adapters: build argv/env for each agent CLI and parse its output.

Flags were verified against OpenClaude 0.31.0, Claude Code 2.1.319 and codex-cli 0.160.0
running against the DAASH LLM gateway (docs/RUNNER.md). Every adapter points its CLI at a
runner-private config folder and scrubs inherited credentials, so a user's own Claude Code /
OpenAI / Anthropic login is never used.
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
)

_INFRA = re.compile(
    r"(api error|http|status|code|error)[:= ]*\s*\"?(401|403|429|5\d\d)\b"
    r"|authentication[_ ]failed|unauthori[sz]ed|invalid[_ ]api[_ ]key|invalid_request_error"
    r"|ECONNREFUSED|ENOTFOUND|ETIMEDOUT|EAI_AGAIN|ECONNRESET|connection (refused|reset|error|closed)"
    r"|unreachable|unable to connect|could not resolve|fetch failed|socket hang up|overloaded"
    r"|service unavailable|bad gateway|gateway timeout|command not found|not found on PATH"
    r"|model gateway|rate limit",
    re.I,
)


def looks_like_infra_error(text: str) -> bool:
    return bool(text and _INFRA.search(text))


@dataclass
class JobSpec:
    model: str
    system_prompt: str
    prompt: str
    state_dir: str                 # runner-private folder for this harness (config/history)
    artifact: str = ""


@dataclass
class Invocation:
    argv: list[str]
    env: dict[str, str]
    stdin: str | None = None


@dataclass
class Parsed:
    final_text: str = ""
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    cost_usd: float | None = None
    outcome: str = "failed"        # submitted | failed | error
    error: str = ""


def _usage(prompt: int, completion: int) -> dict:
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def base_env(hcfg: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Inherited environment without credentials, overlaid with the harness config env."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k not in SCRUBBED_ENV}
    env.update(hcfg.get("env") or {})
    return env


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


# --------------------------------------------------------------------------- Claude-Code-style

def _build_claude_like(hcfg, spec: JobSpec, env: dict[str, str], default_mode: str) -> Invocation:
    argv = [hcfg["command"], "-p", "--output-format", "stream-json", "--verbose",
            "--model", spec.model,
            "--permission-mode", hcfg.get("permission_mode") or default_mode]
    if spec.system_prompt:
        argv += ["--append-system-prompt", spec.system_prompt]
    argv += list(hcfg.get("args") or [])
    # The prompt goes on stdin (no argv length limit).
    return Invocation(argv=argv, env=env, stdin=spec.prompt)


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


# --------------------------------------------------------------------------- fake

def build_fake(hcfg, spec: JobSpec, environ=None) -> Invocation:
    env = base_env(hcfg, environ)
    if spec.artifact and "DAASH_FAKE_ARTIFACT" not in env:
        env["DAASH_FAKE_ARTIFACT"] = spec.artifact
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
    "fake": (build_fake, lambda o, e, c: parse_claude_like(o, e, c, "the fake CLI")),
}


def build(harness: str, hcfg: Mapping[str, Any], spec: JobSpec, environ=None) -> Invocation:
    return ADAPTERS[harness][0](hcfg, spec, environ)


def parse(harness: str, stdout: str, stderr: str, exit_code: int | None) -> Parsed:
    return ADAPTERS[harness][1](stdout, stderr, exit_code)
