"""Relay adapters of an agent host (docs/DESIGN-0.4.md 5.9): hand one DAASH job to one of the
owner's agents through its own CLI. The agent keeps its own model, credentials, memory, tools and
approval rules; DAASH only adds a job folder and a message.

* `openclaw-agent`: the job folder is created inside the agent's own workspace
  (`<workspace>/daash-jobs/<job8>/`) so its file tools reach it. One turn runs through the owner's
  running gateway: `openclaw agent --agent <ref> --session-key daash:<job> --message-file <file>
  --json --timeout <s>` (a session of its own per job; in OpenClaw 2026.10.x gateway turns read
  the message from a file, not `-`). Output: `{"runId", "status": "ok"|"timeout"|..., "result":
  {"payloads": [{"text"}], "meta": {"agentMeta": {"usage"}}}}`, or `{"ok": false, "error":
  {"type": "cli_error", "message"}}` with exit 1 (gateway down, unknown agent, AI service
  unreachable). The folder is removed after the result is uploaded.
* `hermes-agent`: `hermes chat --query-file - --format stream-json --in <job folder> --source tool`
  with the profile's own `HERMES_HOME`. Never `--yolo`: Hermes keeps denying dangerous commands
  on the owner's machine. Parsed like the runner's `hermes` adapter.

Captured outputs: tests/fixtures/agent_hosts/.
"""
from __future__ import annotations

import os
import re
from typing import Mapping, Optional

from . import adapters
from .adapters import Invocation, Parsed, looks_like_infra_error
from .discovery import HERMES, OPENCLAW, LocalAgent

OPENCLAW_RELAY = "openclaw-agent"
HERMES_RELAY = "hermes-agent"
RELAYS = (OPENCLAW_RELAY, HERMES_RELAY)
JOBS_SUBDIR = "daash-jobs"
#: Seconds the agent's own turn deadline is shorter than the runner's, so it can end cleanly.
TIMEOUT_MARGIN = 30
MAX_BLOCK_FILES = 200

_FILE_BLOCK = re.compile(r"<<<FILE\s+([^\n>]+?)\s*>>>\n(.*?)\n?<<<END FILE>>>", re.S)
_DELETE_BLOCK = re.compile(r"<<<DELETE\s+([^\n>]+?)\s*>>>")


def job_folder(relay: str, agent: LocalAgent, job_dir: str, job_id: str) -> str:
    """Where the agent works: inside the OpenClaw agent's workspace, or the runner's own job
    folder for Hermes (whose file tools are not limited to a workspace)."""
    if relay == OPENCLAW_RELAY:
        if not agent.workspace:
            raise ValueError(f"OpenClaw agent '{agent.ref}' has no workspace folder")
        return os.path.join(agent.workspace, JOBS_SUBDIR, job_id[:8])
    return os.path.join(job_dir, "ws")


def message(job: Mapping, folder: str, tools: bool) -> str:
    """The text the agent receives: where to work, which DAASH tools it has, then DAASH's
    system prompt (when the coordinator sent it separately) and the work prompt."""
    jid = job.get("id", "")
    head = [
        f"[DAASH job {jid}] Your working folder for this job is {folder}",
        "It holds a copy of the DAASH swarm's shared workspace. Work only inside this folder: "
        "create and edit the files there with your own file tools and run any commands from "
        "inside it. Do not change files anywhere else. When you finish, DAASH collects every file "
        "you created, changed or deleted in this folder.",
    ]
    if tools:
        head.append(
            "You have DAASH tools (MCP server `daash`): remember/recall for your persistent DAASH "
            "memory, team_roster, send_message/read_messages to coordinate with your team, "
            "current_task, create_subtask, propose_team, list_proposals and vote_on_proposal. "
            f"Pass job={jid} if a DAASH tool asks which job. They have no file tools: your files "
            "are the working folder above.")
    else:
        head.append("This job has no DAASH tools: do not call the `daash` MCP server. Put your "
                    "work in the files of the working folder.")
    system = str(job.get("system_prompt") or "").strip()
    prompt = str(job.get("prompt") or "").strip()
    parts = ["\n".join(head)]
    if system and system[:200] not in prompt:
        parts.append(system)
    parts.append(prompt)
    return "\n\n---\n\n".join(p for p in parts if p)


def build_openclaw_agent(agent: LocalAgent, job: Mapping, message_file: str, env: Mapping[str, str],
                         timeout: int) -> Invocation:
    turn = max(30, int(timeout) - TIMEOUT_MARGIN)
    argv = [agent.cli, "agent", "--agent", agent.ref, "--session-key", f"daash:{job.get('id', '')}",
            "--message-file", message_file, "--json", "--timeout", str(turn)]
    return Invocation(argv=argv, env=dict(env), stdin=None)


def build_hermes_agent(agent: LocalAgent, folder: str, env: Mapping[str, str], text: str
                       ) -> Invocation:
    run_env = dict(env)
    run_env["HERMES_HOME"] = agent.home
    argv = [agent.cli, "chat", "--query-file", "-", "--format", "stream-json", "--in", folder,
            "--source", "tool"]
    return Invocation(argv=argv, env=run_env, stdin=text)


def _usage(meta: Mapping) -> dict:
    u = ((meta.get("agentMeta") or {}).get("usage") or {}) if isinstance(meta, Mapping) else {}
    if not isinstance(u, Mapping):
        u = {}
    prompt = adapters._int(u.get("input")) + adapters._int(u.get("cacheRead")) \
        + adapters._int(u.get("cacheWrite"))
    return adapters._usage(prompt, adapters._int(u.get("output")))


def parse_openclaw_agent(stdout: str, stderr: str, exit_code: Optional[int]) -> Parsed:
    env = adapters._json_envelope(stdout)
    if env is None:
        return adapters._no_result(stdout, stderr, exit_code, "openclaw agent")
    if env.get("ok") is False or ("error" in env and "result" not in env):
        err = env.get("error") if isinstance(env.get("error"), Mapping) else {}
        text = str(err.get("message") or "openclaw reported an error")
        p = Parsed(error=text[:1000])
        # gateway down, AI service unreachable, sign-in problems: infrastructure, no attempt;
        # an unknown agent id means the agent left this host: also not the agent's failure
        p.outcome = "error" if (looks_like_infra_error(text + "\n" + stderr[-2000:])
                                or "unknown agent" in text.lower()
                                or "gateway" in text.lower()) else "failed"
        return p
    result = env.get("result") if isinstance(env.get("result"), Mapping) else {}
    payloads = [x for x in result.get("payloads") or [] if isinstance(x, Mapping)]
    final = "\n".join(str(x.get("text") or "") for x in payloads if not x.get("isError")).strip()
    errors = "\n".join(str(x.get("text") or "") for x in payloads if x.get("isError")).strip()
    p = Parsed(final_text=final, usage=_usage(result.get("meta") or {}))
    status = str(env.get("status") or "")
    rerr = result.get("error") if isinstance(result.get("error"), Mapping) else {}
    message = str(rerr.get("message") or errors or "")
    if status == "timeout":
        p.outcome = "error" if looks_like_infra_error(message) else "failed"
        p.error = ("the agent's turn timed out" + (f": {message}" if message else ""))[:1000]
    elif status not in ("ok", ""):
        p.error = (message or f"openclaw ended with status '{status}'")[:1000]
        p.outcome = "error" if looks_like_infra_error(p.error) else "failed"
    elif exit_code not in (0, None):
        p.outcome, p.error = "failed", f"openclaw agent exited with code {exit_code}"
    elif not final:
        p.outcome, p.error = "failed", (message or "the agent ended without a final answer")[:1000]
    else:
        p.outcome = "submitted"
    return p


def parse(relay: str, stdout: str, stderr: str, exit_code: Optional[int]) -> Parsed:
    if relay == OPENCLAW_RELAY:
        return parse_openclaw_agent(stdout, stderr, exit_code)
    return adapters.parse_hermes(stdout, stderr, exit_code)


def _safe_rel(path: str) -> Optional[str]:
    path = path.strip().strip("`'\"")
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        return None
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def apply_file_blocks(text: str, folder: str) -> tuple:
    """Write `<<<FILE path>>> ... <<<END FILE>>>` blocks of a reply into the job folder and
    apply `<<<DELETE path>>>` (for agents that answer with their files instead of writing
    them). Unsafe paths are skipped. Returns (written, deleted)."""
    written, deleted = [], []
    root = os.path.realpath(folder)
    for m in _FILE_BLOCK.finditer(text or ""):
        rel = _safe_rel(m.group(1))
        if rel is None or len(written) >= MAX_BLOCK_FILES:
            continue
        target = os.path.realpath(os.path.join(root, rel))
        if not target.startswith(root + os.sep):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(m.group(2) + ("\n" if not m.group(2).endswith("\n") else ""))
        written.append(rel)
    for m in _DELETE_BLOCK.finditer(text or ""):
        rel = _safe_rel(m.group(1))
        if rel is None:
            continue
        target = os.path.realpath(os.path.join(root, rel))
        if target.startswith(root + os.sep) and os.path.isfile(target):
            os.remove(target)
            deleted.append(rel)
    return written, deleted


__all__ = ["RELAYS", "OPENCLAW_RELAY", "HERMES_RELAY", "job_folder", "message",
           "build_openclaw_agent", "build_hermes_agent", "parse", "parse_openclaw_agent",
           "apply_file_blocks", "OPENCLAW", "HERMES"]
