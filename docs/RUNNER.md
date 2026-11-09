# DAASH runner (0.3)

The runner lets a team's Mission Delivery agents work through an external agent CLI (OpenClaude,
Claude Code, Codex) on any device. It is **pull-based**: the runner dials out to the coordinator, so it
works behind NAT or over Tailscale. Contract: docs/DESIGN-0.3.md sections 5 and 7. Code: `daash_runner/`
(Python 3.10+, standard library only: no venv, no `pip install`).

## What it does

hello (announce harnesses and models) -> long-poll `lease` while a slot is free -> download the swarm
workspace tar -> safe extract into `<work-dir>/jobs/<job_id>/ws/` -> snapshot (sha256) -> run the CLI in
a new process group -> heartbeat every 15 s -> diff the folder (changed, new, deleted; `.git/ .claude/
.codex/ .openclaude/ node_modules/ .daash/` ignored) -> upload result. A `cancelled` heartbeat or the job
timeout kills the CLI's whole process group. SIGINT/SIGTERM stops running CLIs and reports `error` for
them. Job folders (with `stdout.log`/`stderr.log`) are kept 3 days. Log: `<work-dir>/runner.log` (rotating).

Outcomes: `submitted` (CLI finished with a final answer), `failed` (no answer, non-zero exit, timeout,
result over the job limits; counts as an attempt) and `error` (infrastructure: CLI missing, gateway
401/403/429/5xx or unreachable; does not count an attempt).

## Run in Docker (recommended: agent-run code stays off the host)

```
# .env: DAASH_LLM_API_KEY=<gateway key>, DAASH_RUNNER_BOOTSTRAP_TOKEN=daash_rt_<random>
docker compose up --build -d                     # coordinator (creates runner 'local-runner' from the token)
docker compose --profile runner up --build -d runner
docker compose logs -f runner
```
The `runner` service (profile `runner`, not started by default) is built from `Dockerfile.runner`
(node 22 + python3 + the three CLIs pinned to the verified versions), runs as a non-root user, sits on the
`frontend` network (egress to the gateway, reaches `http://coordinator:8000`), uses `runner/runner.docker.json`
and keeps its work dir in the `runner-work` volume. The coordinator service must receive the same
`DAASH_RUNNER_BOOTSTRAP_TOKEN` for the bootstrap runner to exist.

## Run on a device (host)

1. In the DAASHboard Runners panel (or `POST /runners {"name": "mac-mini"}`) create a runner; copy the
   token (shown once).
2. Copy `runner/runner.example.json` to `runner.json` and edit (see below). Make sure the CLIs you list are
   on `PATH`; only those found are announced.
3. Start:
```
python3 -m daash_runner --coordinator http://host:8000 --token daash_rt_... \
    --config runner.json --work-dir ~/.daash-runner --max-jobs 2
```
Every flag also reads an env var: `DAASH_COORDINATOR_URL`, `DAASH_RUNNER_TOKEN`, `DAASH_RUNNER_CONFIG`,
`DAASH_RUNNER_WORKDIR`, `DAASH_RUNNER_MAX_JOBS` (and `DAASH_RUNNER_HOST` for the reported host name).
A 401 at startup exits with code 2; later 401s and coordinator outages are retried with exponential backoff
(1 s up to 60 s).

## Config file

```json
{"name": "mac-mini",
 "harnesses": {
   "codex": {"command": "codex", "models": ["fast", "great"], "sandbox": "workspace-write",
             "env": {"DAASH_GATEWAY_URL": "http://gw:12345/v1", "DAASH_LLM_API_KEY": "${DAASH_LLM_API_KEY}"},
             "args": [], "timeout_seconds": 1800}}}
```
* `models`: models this harness accepts; empty = any. Jobs for other models are not leased.
* `capabilities`: what this harness can do for a job: `edit_files`, `run_code`, `web_research`. The
  coordinator only leases a job to a harness entry that offers the job's `required_capabilities` (a task
  that must run code never goes to an entry without `run_code`). Default when omitted: `edit_files` +
  `run_code`, except `edit_files` only for a claude-code/openclaude entry whose `permission_mode` is
  `acceptEdits`, `default` or `plan` (those modes cannot run shell commands unattended) and for codex with
  `sandbox: "read-only"`. Hello announces it per harness: `{"harness", "models", "capabilities"}`; an old
  runner that omits it is treated as the harness default.
* `env`: extra environment; `${NAME}` expands from the runner's own environment (unset becomes empty).
* `args`: extra CLI arguments inserted before the prompt. `timeout_seconds` caps a job (the job's own
  limit applies if smaller).
* Harness-specific keys: `permission_mode` (openclaude, claude-code), `small_model` (claude-code, default
  `instant`), `sandbox` (codex: `workspace-write` default, `read-only`, `danger-full-access`, or `bypass`),
  `api_key_env` (codex, default `DAASH_LLM_API_KEY`).

## Verified CLI invocations

All verified on 2026-11-08 inside the runner image against the m3u512gb gateway with model `fast`
(prompt "Create hello.md containing the word hello, then reply DONE", ~20 s each, all created the file).
Versions: **OpenClaude 0.31.0, Claude Code 2.1.319, codex-cli 0.160.0** (pinned in `Dockerfile.runner`).
The prompt is always sent on stdin; the system prompt goes in as shown.

**claude-code**
```
claude -p --output-format stream-json --verbose --model <m> --permission-mode bypassPermissions \
       --append-system-prompt "<system>"                      # prompt on stdin
```
Env: `ANTHROPIC_BASE_URL=<gateway root, no /v1>`, **`ANTHROPIC_AUTH_TOKEN=<key>`** (the gateway wants
`Authorization: Bearer`; `ANTHROPIC_API_KEY` is sent as `x-api-key` and gets 401), `CLAUDE_CONFIG_DIR=<runner
private>`, `ANTHROPIC_DEFAULT_HAIKU_MODEL=ANTHROPIC_SMALL_FAST_MODEL=instant` (background model),
`ANTHROPIC_DEFAULT_SONNET_MODEL/OPUS_MODEL=<m>`, `DISABLE_TELEMETRY`, `DISABLE_AUTOUPDATER`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`. Output: stream-json; last line `{"type":"result",
"is_error", "result", "usage", "total_cost_usd"}`. Seen: 11,565 in + 22,592 cached in / 363 out. It prints a
harmless `unrecognized_model` notice for gateway aliases. Refuses `bypassPermissions` as root (the image uses
a non-root user).

**openclaude**
```
openclaude -p --output-format stream-json --verbose --model <m> --permission-mode fullAccess \
           --append-system-prompt "<system>"                  # prompt on stdin
```
Env: `CLAUDE_CODE_USE_OPENAI=1`, `OPENAI_BASE_URL=<gateway>/v1`, `OPENAI_API_KEY=<key>`,
`OPENCLAUDE_CONFIG_DIR` and `CLAUDE_CONFIG_DIR=<runner private>` (without it OpenClaude writes `~/.openclaude`).
Same stream-json shape as Claude Code. Seen: 11,583 in + 11,520 cached / 120 out.

**codex**
```
codex exec --json --skip-git-repo-check --sandbox workspace-write -m <m> \
  -c 'model_provider="daash"' \
  -c 'model_providers.daash={name="daash", base_url="<gateway>/v1", env_key="DAASH_LLM_API_KEY", wire_api="responses"}' \
  -c 'web_search="disabled"' -c 'developer_instructions="<system>"' -        # prompt on stdin
```
Env: `CODEX_HOME=<runner private>` and the key variable named by `env_key`. `web_search="disabled"` is
required: the gateway rejects the hosted web_search tool ("only function tools are supported"). Output: JSONL
(`item.completed` with `agent_message` = final text, `turn.completed.usage`, or `turn.failed`). Seen: 17,502 in /
138 out. **Sandbox**: `--sandbox workspace-write` needs bubblewrap and user namespaces, which Docker does not
allow (`bwrap: No permissions to create a new namespace`, commands and `apply_patch` fail), so the container
config uses `"sandbox": "bypass"` (`--dangerously-bypass-approvals-and-sandbox`); the container is the
sandbox. On a host keep `workspace-write`.

Every adapter removes inherited `ANTHROPIC_*`, `OPENAI_*`, `CODEX_*`, `CLAUDE_*` credential/endpoint variables
from the job environment before applying the config, so a user's own Claude Code login, OpenAI account or
Anthropic key is never used.

## Security notes

* These CLIs **execute code** chosen by an LLM with the permissions of the runner's user. Prefer the
  container. In it, the only things at risk are the job folder and the gateway key in its environment.
* A host runner should run as a dedicated unprivileged user and prefer each CLI's own sandbox.
  `runner/runner.example.json` is conservative on purpose: `codex` uses `workspace-write`, and Claude Code /
  OpenClaude use `acceptEdits` (file edits only; shell commands would need approval, which headless mode
  cannot give, so the agent works without running code). Switch them to `bypassPermissions` /
  `fullAccess` only on a dedicated machine or VM. The container config (`runner/runner.docker.json`) uses
  full access because the container is the sandbox.
* The runner token authorises leasing jobs and downloading the swarm workspace; treat it as a secret and
  disable or delete the runner in the DAASHboard to revoke it. Coordinator URLs should be on a trusted
  network (Tailscale) or HTTPS: tokens travel in a header.
* Workspace tars are extracted with absolute paths, `..`, links and special files rejected.

## Troubleshooting

* Runner not in the Runners panel / offline: check `<work-dir>/runner.log`; "401" = wrong, disabled or
  deleted token; "coordinator error" = URL/network (backoff up to 60 s).
* A harness missing from the panel: its `command` is not on `PATH` for the runner process.
* Jobs end with `error` and "401/authentication_failed": for Claude Code use `ANTHROPIC_AUTH_TOKEN`, not
  `ANTHROPIC_API_KEY`; check `DAASH_LLM_API_KEY` reached the container (`docker compose exec runner env`).
* Codex "only function tools are supported": `web_search="disabled"` missing from `args`/adapter.
* Codex "bwrap: No permissions to create a new namespace": use `"sandbox": "bypass"` inside Docker.
* Job stuck: look at `<work-dir>/jobs/<job_id>/stdout.log` and `stderr.log`; the timeout kills the CLI.
* `docker build -f Dockerfile.runner` is the only supported way to get the CLIs on the container path.
