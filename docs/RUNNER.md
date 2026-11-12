# DAASH runner (0.4)

The runner lets a team's Mission Delivery agents work through an external agent CLI (OpenClaude,
Claude Code, Codex, OpenClaw, Hermes Agent) on any device. It is **pull-based**: the runner dials out to
the coordinator, so it works behind NAT or over Tailscale. Contract: docs/DESIGN-0.3.md sections 5 and 7,
docs/DESIGN-0.4.md sections 4 and 5.9. Code: `daash_runner/` (Python 3.9+, standard library only: no
venv, no `pip install`).

Every CLI the runner launches gets **DAASH's tools over MCP** for its job (memory, team messages, the
team roster, subtasks, team proposals and votes; see "DAASH tools" below).

The same runner also turns a machine into an **agent host**: one command connects that machine's
existing OpenClaw agents and Hermes Agent profiles to DAASH, even behind a firewall. See
"Agent hosts" below, and docs/AGENT-HOSTS.md for the owner's step-by-step guide.

## What it does

hello (announce harnesses and models) -> long-poll `lease` while a slot is free -> download the swarm
workspace tar -> safe extract into `<work-dir>/jobs/<job_id>/ws/` -> snapshot (sha256) -> run the CLI in
a new process group -> heartbeat every 15 s -> diff the folder (changed, new, deleted; `.git/ .claude/
.codex/ .openclaude/ .openclaw/ .hermes/ node_modules/ .daash/` ignored) -> upload result. A `cancelled`
heartbeat or the job timeout kills the CLI's whole process group. SIGINT/SIGTERM stops running CLIs and
reports `error` for them. Job folders (with `stdout.log`/`stderr.log`, and `cli/` for the config the
runner generated for that job's CLI) are kept 3 days. Log: `<work-dir>/runner.log` (rotating).

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
(node 24 + python3 + the five CLIs pinned to the verified versions, `tini` as PID 1), runs as a non-root
user, sits on the
`frontend` network (egress to the gateway, reaches `http://coordinator:8000`), uses `runner/runner.docker.json`
and keeps its work dir in the `runner-work` volume. The coordinator service must receive the same
`DAASH_RUNNER_BOOTSTRAP_TOKEN` for the bootstrap runner to exist.

## Run on a device (host)

1. In DAASHboard's Agents & models page, Runners panel (or `POST /runners {"name": "mac-mini"}`) create a runner; copy the
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
  `api_key_env` (codex, openclaw, hermes: the env var holding the gateway key, default
  `DAASH_LLM_API_KEY`), `thinking` (openclaw `--thinking` level), `context_window` / `max_tokens`
  (openclaw model metadata, default 131072 / 16384), `max_turns` (hermes, default 90),
  `context_length` (hermes, default 131072). openclaw and hermes take the gateway from
  `DAASH_GATEWAY_URL` in `env` (the OpenAI-compatible `/v1` URL), as codex does.

## DAASH tools (MCP)

Every CLI the runner launches gets DAASH's MCP server, named `daash`, for its job (docs/DESIGN-0.4.md 4.2).
The lease carries `"mcp": {"path": "/mcp", "token": "djt_..."}`; the runner builds the URL from **its own**
coordinator URL (`--coordinator` + path), so the tools work wherever the runner reaches the coordinator. The
job token goes into the CLI's environment as `DAASH_MCP_TOKEN`; the per-job config files refer to it
(`${DAASH_MCP_TOKEN}`) and never contain it. It stops working when the job ends. A lease without `mcp` (a
0.3 coordinator) runs the CLI without DAASH tools. The tools then appear to the agent as `remember`,
`recall`, `team_roster`, `send_message`, `read_messages`, `create_subtask`, `propose_team`,
`vote_on_proposal`, `list_proposals`, `current_task` (Claude Code shows them as `mcp__daash__<tool>`).

| CLI | How it gets the `daash` server |
|---|---|
| claude-code, openclaude | `--mcp-config <job>/cli/mcp.json --strict-mcp-config --allowedTools mcp__daash`; the file holds `{"mcpServers": {"daash": {"type": "http", "url": URL, "headers": {"Authorization": "Bearer ${DAASH_MCP_TOKEN}"}}}}`. `--strict-mcp-config` ignores any other MCP config (a workspace `.mcp.json`). `--allowedTools mcp__daash` is **required** whenever the permission mode is not a bypass mode: without it `-p` answers "Claude requested permissions to use mcp__daash__remember, but you haven't granted it yet". |
| codex | `-c mcp_servers.daash.url="URL" -c mcp_servers.daash.bearer_token_env_var="DAASH_MCP_TOKEN" -c mcp_servers.daash.default_tools_approval_mode="approve"`. The approval mode is **required** with `--sandbox workspace-write`: without it the call fails with "MCP tool call requires approval, but approval policy is never". |
| openclaw | `mcp.servers.daash: {url, transport: "streamable-http", headers: {Authorization: "Bearer ${DAASH_MCP_TOKEN}"}}` in the job's `--config` file (OpenClaw substitutes `${VAR}`). |
| hermes | `mcp_servers.daash: {url, headers: {Authorization: "Bearer ${DAASH_MCP_TOKEN}"}, timeout, connect_timeout}` in the job's `$HERMES_HOME/config.yaml`. |
| fake | `DAASH_MCP_URL` and `DAASH_MCP_TOKEN` in the environment. With `DAASH_FAKE_MCP=remember,send_message` (or `all`) it calls those tools over stdlib JSON-RPC before writing the artifact (`DAASH_FAKE_MCP_TO`, default `team`; `DAASH_FAKE_MCP_STRICT=1` fails the run if a call fails). |

Verified on 2026-11-11 in the runner image: each of the five CLIs listed the tools of a stub MCP server
(`tests/fakes/fake_mcp_server.py`, streamable HTTP, bearer token) and called `remember` when asked to,
with the config above (claude-code in both `bypassPermissions` and `acceptEdits`, codex in both `bypass` and
`workspace-write`).

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

**openclaw** (OpenClaw **2026.11.3**, npm `openclaw`; needs node >= 24.16; verified 2026-11-11, ~25-30 s)
```
openclaw agent exec --config <job>/cli/openclaw/openclaw.json --state-dir <job>/cli/openclaw/state \
  --message-file - --cwd <job>/ws --json --model daash/<m> --timeout <job timeout - 30>   # message on stdin
```
`agent exec` runs one embedded turn without a gateway. It has no system-prompt option, so the message is the
system prompt, `---`, then the prompt. The per-job config has `models.mode: "replace"` with one provider
`daash` (`baseUrl` = `DAASH_GATEWAY_URL`, `api: "openai-completions"`, `apiKey: "${DAASH_LLM_API_KEY}"`,
one model entry per configured model with `compat.supportsUsageInStreaming: true`, without which the gateway
reports no token usage), `agents.defaults.model.primary = daash/<m>` and the `daash` MCP server. Env:
`OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`, `OPENCLAW_CONFIG_PATH` = per-job folders (so neither `~/.openclaw` nor
its plugins or credentials are read), `OPENCLAW_NO_AUTO_UPDATE=1`. Output: one pretty-printed JSON envelope
`{"ok", "status": "ok"|"error"|"timeout", "final", "payloads", "usage": {"input", "output", "cacheRead",
"cacheWrite", ...}, "error": {"message", "kind"}}`; `input` excludes cached input. Exit 0 ok, 1 error, 2
timeout. Outcomes: exit 2/`timeout` = failed, except "Couldn't connect to the AI service" (the gateway is
unreachable, which also ends as a timeout) = error; a bad key ("Couldn't sign in to the AI service", stderr
`reason=auth ... 401`) = error. Seen: 9,321 in + 18,336 cached / 405 out. Files it writes are mode 0600.

**hermes** (Hermes Agent **v0.23.1**, commit `3b421578`; verified 2026-11-11, ~40-45 s of which ~20 s is setup)
```
hermes chat --query-file - --format stream-json --yolo --in <job>/ws -m <m> --max-turns 90 --source tool
```
Env `HERMES_HOME=<job>/cli/hermes-home`, whose `config.yaml` (written as JSON, which YAML reads as is) sets
`model: {provider: custom, base_url: DAASH_GATEWAY_URL, key_env: DAASH_LLM_API_KEY, default: <m>,
context_length}`, `agent.system_prompt` (DAASH's system prompt), `terminal.backend: local` (the container is
the sandbox), memory and user profile off, `agent.disabled_toolsets: [memory, session_search, skills,
cronjob]` (DAASH memory is the shared one; a job leaves no skills or cron jobs behind),
`auth.adopt_external_logins: false` (Hermes would otherwise borrow a Codex or Claude Code login), update checks
and telemetry off, and the `daash` MCP server. Since v0.22 the CLI is a client of a gateway per
`HERMES_HOME`, which it starts in the background and which outlives it (killing the CLI does not stop the
turn). After every job (also on timeout or cancel) the runner runs `hermes gateway stop --yes` with the
job's `HERMES_HOME` (without `--yes` it refuses while a killed CLI's turn is still running; never `--all`,
which would stop every gateway of the user), then deletes the home's `installs/` and `cache/`. A per-job home is a "borrowing" data root: on its first command Hermes prepares
its own dependency environment (~20 s, ~420 MB, removed after the job). A named profile under a shared home
would avoid that, but profiles share one gateway and read keys only from their own `.env` file, so DAASH
uses per-job homes. Output: JSONL `system`, `text`, `tool_use`, `tool_result`, then `{"type": "result",
"exit_code", "text", "tokens": {"input", "output", "cache_read", ...}, "error"?}`; `input` includes cached
input. Exit 3 (gateway owner lost) = error; a result `error` that looks like infrastructure ("No API key ...",
401, unreachable) = error. Seen: 31,744 in (20,960 cached) / 322 out.

Every adapter removes inherited `ANTHROPIC_*`, `OPENAI_*`, `CODEX_*`, `CLAUDE_*`, `OPENCLAW_*` home/config,
`HERMES_HOME` and other credential/endpoint variables from the job environment before applying the config,
so a user's own Claude Code login, OpenAI account, Anthropic key, OpenClaw or Hermes setup is never used.

## Agent hosts

An agent host is a machine whose existing agents (an OpenClaw agent like Fitz, a Hermes Agent
profile) work for DAASH, although nobody can connect to the machine (docs/DESIGN-0.4.md 5.9). The
runner on it dials out, finds the local agents and relays DAASH jobs to them through their own
CLIs. The agents keep their own model, credentials and tools. The owner's guide, with a worked
macOS example, is docs/AGENT-HOSTS.md.

**The one command.** DAASHboard (or `POST /runner-enrollments` on the admin port) mints a one-time
code, valid for 60 minutes, and shows the owner's command. It needs `DAASH_AGENT_PUBLIC_URL`:
```
curl -fsSLo ~/daash-runner.pyz <PUBLIC>/runner/daash-runner.pyz && python3 ~/daash-runner.pyz connect <PUBLIC> <CODE>
```
`daash-runner.pyz` is this package as a zipapp. It is built into the coordinator image
(`python -m daash_runner.pyz OUT`) and served on the agent port without a token. Subcommands:

| Command | What it does |
|---|---|
| `connect <url> <code> [--no-tools] [--no-service] [--name N]` | Exchanges the code for a runner token (`POST /runner/enroll`) and writes `~/.daash-runner/config.json` (0600). Then it discovers the agents, says hello, gives each new agent DAASH's tools and installs and starts a user service. It prints what it found and changed. |
| `status` | DAASH reachable?, service state, shared agents and their tools |
| `run` | The runner loop (what the service runs). Logs go to `~/.daash-runner/logs/runner.log`. |
| `disconnect` | Stops and removes the service, removes exactly the tool entries `connect` recorded, tells DAASH (`POST /runner/disconnect`: runner disabled, agents offline), and deletes the config and job folders. It keeps the logs. |

**Service.**
* macOS: the LaunchAgent `~/Library/LaunchAgents/org.daash.runner.plist`.
  * Settings: `RunAtLoad`, `KeepAlive`, the PATH of the owner's terminal, and output in
    `~/.daash-runner/logs/service.{out,err}.log`.
  * Started with `launchctl bootstrap gui/<uid>`, falling back to `load -w`.
* Linux: the systemd user unit `~/.config/systemd/user/daash-runner.service`, started with
  `systemctl --user enable --now`.
* Both run `python3 ~/daash-runner.pyz run` with the `python3` that ran `connect`.

**Discovery** runs on every hello, every 5 minutes. Agent CLIs run with a PATH made of, in order:
* the runner's own PATH;
* the PATH saved at `connect`;
* the owner's login-shell PATH;
* the usual install folders (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.npm-global/bin`,
  `~/.local/bin`, nvm, volta, bun).

The CLIs get the owner's environment, minus the runner's own secrets.
* **OpenClaw:** `openclaw agents list --json` gives each agent's id, identity name, workspace and
  whether it is the default. `GET http://127.0.0.1:<gateway.port>/health` must answer
  `{"ok":true,...}`, because relayed turns go through the running gateway. A dead gateway is
  reported as not available.
* **Hermes:** `hermes profile list` gives the default profile (`$HERMES_HOME`, default `~/.hermes`)
  and the named profiles (`<home>/profiles/<name>`).
* `share_agents` in the config limits what is offered. Entries can be an id, `kind:id`, or a name.

The hello carries `"agents": [{"kind", "ref", "name", "available", "detail", "tools", "new_token"?}]`.
The coordinator answers with the agents as DAASH knows them, plus `agent_tokens`
(`{local_ref: "dca_..."}`) for agents that are new, adopted or asked for a new token. Each token is
sent once. An agent host announces no harnesses unless its config lists some, so it never runs
DAASH's own CLI jobs.

**DAASH tools for the agents.** These are the connected-agent MCP tokens of docs/DESIGN-0.4.md 2.3:
memory, messages, roster, subtasks and proposals, but no workspace file tools, because the agent
works on a local copy.
* **OpenClaw:**
  * Added with
    `openclaw mcp add daash --url <coordinator>/mcp --transport streamable-http --header "Authorization=Bearer dca_..."`.
    OpenClaw probes the server before saving. A running 2026.10.9 gateway uses it from the next
    turn on, with no restart.
  * The tools appear as `daash__remember` and so on.
  * OpenClaw's MCP servers are shared by all of its agents, so one OpenClaw install gets one `daash`
    server. It goes to the default agent, or else the first shared one. The other agents work
    without DAASH tools, and their prompt says so.
* **Hermes:**
  * Added with
    `hermes config set mcp_servers.daash.url <coordinator>/mcp` and
    `hermes config set mcp_servers.daash.headers.Authorization 'Bearer ${DAASH_MCP_TOKEN}'`, with
    `HERMES_HOME` set to the profile. Hermes's own writer keeps the file's comments.
  * The token itself goes into the profile's `.env` (mode 0600), as `DAASH_MCP_TOKEN=...` under a
    marker comment.
  * The tools appear as `mcp__daash__remember` and so on.
* A `daash` server that points elsewhere is never replaced. A failed install (for example the probe
  could not reach DAASH) is retried at the next hello.

**Work.** A `runner` connected agent's job is a `connected` remote job. Its payload holds
`connected_agent_id`, `protocol: "runner"`, `runner_id`, `relay` (`openclaw-agent` | `hermes-agent`),
`local_ref`, plus `prompt`, `system_prompt` (optional), `limits`, `task` and `checks`.
* Only that runner leases it, before its harness jobs, and without a job MCP token.
* Heartbeats, the workspace download, the result and command checks work as for any job.
* An expired lease is requeued for the same runner.

The runner sends the agent one message:
1. a header naming the job id and the absolute job folder ("work only inside it"), and saying
   whether it has DAASH tools;
2. DAASH's system prompt, unless the prompt already contains it;
3. the work prompt.

If the reply carries `<<<FILE path>>> ... <<<END FILE>>>` or `<<<DELETE path>>>` blocks, they are
applied to the folder before the diff.

**`openclaw-agent`** (verified with OpenClaw 2026.10.9, the version on Fitz's Mac):
```
openclaw agent --agent <id> --session-key daash:<job id> --message-file <job>/message.md --json --timeout <job timeout - 30>
```
* The job folder is `<agent workspace>/daash-jobs/<job id[:8]>/`, so the agent's file tools reach
  it. It is removed after the result is built.
* Gateway turns in 2026.10.9 cannot read the message from stdin: `--message-file -` gives "Message
  file not found: -". So the message goes in a 0600 file in the runner's job folder.
* The session key gives every job its own session, `agent:<id>:daash:<job id>`.
* The turn runs in the owner's gateway, with the agent's own model, persona files and tools.
* Output:
  ```
  {"runId", "status": "ok"|"timeout"|..., "summary",
   "result": {"payloads": [{"text", "isError"?}],
              "meta": {"agentMeta": {"usage": {"input", "output", "cacheRead", ...}}}}}
  ```
  The final text is the joined non-error payloads.
* A failure gives `{"ok": false, "error": {"type": "cli_error", "message"}}` and exit 1. How each
  failure counts:

  | What happened | Outcome |
  |---|---|
  | `Gateway not reachable at ws://127.0.0.1:18789 (ECONNREFUSED)` | `error` |
  | `Couldn't connect to the AI service` | `error` |
  | `Unknown agent id "x"` | `error` |
  | A turn timeout (`status: "timeout"`) | `failed` |

**`hermes-agent`** (Hermes Agent 0.23.1):
```
hermes chat --query-file - --format stream-json --in <job folder> --source tool
```
* It runs with `HERMES_HOME` set to the profile's folder. The job folder is
  `~/.daash-runner/jobs/<job id>/ws`.
* There is no `--yolo`: Hermes keeps denying dangerous commands on the owner's machine. File writes
  and ordinary commands work.
* The owner's own gateway for that profile serves the turn. The runner never stops it.
* The output is parsed like the `hermes` adapter's.

Both relays were verified end to end on 2026-11-12 in a Linux test agent host:
1. The owner command (`--no-service`) enrolled the host and found one OpenClaw agent and two Hermes
   profiles.
2. It added the tools.
3. One relay job per kind wrote its file into the DAASH workspace, called `remember` over MCP, and
   passed a command check.
4. `disconnect` removed everything it had added.

The captured CLI outputs are in `tests/fixtures/agent_hosts/`.

## Security notes

* These CLIs **execute code** chosen by an LLM with the permissions of the runner's user. Prefer the
  container. In it, the only things at risk are the job folder and the gateway key in its environment.
* A host runner should run as a dedicated unprivileged user and prefer each CLI's own sandbox.
  `runner/runner.example.json` is conservative on purpose: `codex` uses `workspace-write`, and Claude Code /
  OpenClaude use `acceptEdits` (file edits only; shell commands would need approval, which headless mode
  cannot give, so the agent works without running code). Switch them to `bypassPermissions` /
  `fullAccess` only on a dedicated machine or VM. The container config (`runner/runner.docker.json`) uses
  full access because the container is the sandbox.
* `openclaw` and `hermes` run unsandboxed (OpenClaw's `agent exec` uses the full headless execution policy;
  Hermes runs with `--yolo` and the `local` terminal backend), so they are in the container config only and
  not in `runner/runner.example.json`. On a host, offer them only on a dedicated machine or VM.
* The runner token authorises leasing jobs and downloading the swarm workspace; treat it as a secret and
  disable or delete the runner in the DAASHboard to revoke it. Coordinator URLs should be on a trusted
  network (Tailscale) or HTTPS: tokens travel in a header.
* Workspace tars are extracted with absolute paths, `..`, links and special files rejected.
* The DAASH MCP token (`DAASH_MCP_TOKEN`) is valid only for its job while the job runs. Code the agent runs
  can read it (as it can read the gateway key); it can only act as that agent on that task.

## Troubleshooting

* Runner not in the Runners panel (Agents & models page) / offline: check `<work-dir>/runner.log`; "401" = wrong, disabled or
  deleted token; "coordinator error" = URL/network (backoff up to 60 s).
* A harness missing from the panel: its `command` is not on `PATH` for the runner process.
* Jobs end with `error` and "401/authentication_failed": for Claude Code use `ANTHROPIC_AUTH_TOKEN`, not
  `ANTHROPIC_API_KEY`; check `DAASH_LLM_API_KEY` reached the container (`docker compose exec runner env`).
* Codex "only function tools are supported": `web_search="disabled"` missing from `args`/adapter.
* Codex "bwrap: No permissions to create a new namespace": use `"sandbox": "bypass"` inside Docker.
* Job stuck: look at `<work-dir>/jobs/<job_id>/stdout.log` and `stderr.log`; the timeout kills the CLI.
* `docker build -f Dockerfile.runner` is the only supported way to get the CLIs on the container path.
  Hermes Agent is installed by its official installer pinned to the release commit (`HERMES_COMMIT` build
  arg), as the runner user, in `~/.hermes` (about 2.3 GB: its own Python, node and tools).
* An agent says it cannot use the DAASH tools: check the job's `cli/` folder and `stderr.log`; Claude Code
  needs `--allowedTools mcp__daash` outside bypass mode, Codex needs `default_tools_approval_mode`
  outside `bypass` (both set by the adapters). A coordinator older than 0.4 sends no `mcp` block.
* Hermes jobs start ~20 s slower than the others: that is Hermes preparing the job's own dependency
  environment (see above). `hermes ... gateway` processes left behind: the runner stops them after every
  job; in the container `tini` reaps them.
* Agent hosts:
  * `python3 ~/daash-runner.pyz status` shows DAASH's reachability, the service and each agent's
    tools. Logs are in `~/.daash-runner/logs/`.
  * A code "already used" or "expired": create a new one (60 minutes, works once).
  * An agent "NOT available": its OpenClaw gateway does not answer on loopback
    (`openclaw gateway status`).
  * "OpenClaw already has an MCP server named 'daash'": someone else's server is kept, and the
    agent works without DAASH tools.
  * More in docs/AGENT-HOSTS.md.
