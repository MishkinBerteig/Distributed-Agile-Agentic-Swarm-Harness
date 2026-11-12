# DAASH 0.4 — DAASH tools for every agent, OpenClaw and Hermes Agent, connected agents (design contract)

0.4 gives **every agent DAASH calls the same DAASH tools** (memory, subtasks, team proposals and votes, and
messages to team members) through an **MCP server**. It adds **OpenClaw** and **Hermes Agent** as agent types the
runner can launch, and it lets DAASH work with **agents that already exist and run elsewhere** ("connected
agents"), such as the user's OpenClaw agents Jane and Fitz. Everything in docs/DESIGN-0.1.md to DESIGN-0.3.md
still holds unless changed here.

Why: the user's direction from the 2026-11-09 prompt review ("memory is critical"; every agent should get DAASH's
tools; teams should be able to message one another) and their 2026-11-11 request: "make sure that OpenClaw and
Hermes Agent can be used in this next version ... it's important to allow DAASH to connect to already-existing
agents".

Facts this design relies on (checked 2026-11-11; notes in `agents-workspace/v0.4-discovery/`):
* OpenClaw 2026.10.x ships an **A2A 1.0 channel** (`channels.a2a`): a public Agent Card at
  `/.well-known/agent-card.json`, JSON-RPC at `/a2a/v1` (`SendMessage`, `GetTask`; `message/send`, `tasks/get`
  aliases), a separate bearer token per peer (`peers.<name>.token`), its own session for each peer and
  `contextId`, `configuration.returnImmediately` and polling with `GetTask`. Text parts only. Inbound text is capped
  at 64 KiB and responses at 1 MiB. Limit: 30 requests per minute per peer. `CancelTask` is refused (`-32004`).
  Tasks live in memory for up to 24 h and are lost on a gateway restart. Jane and Fitz already talk to each other this way.
* OpenClaw is an **MCP client** (`mcp.servers.<name>`: `url`, `transport: "streamable-http"`, `headers`).
  `openclaw agent exec --message-file - --cwd DIR --json --model P/M --timeout S` runs one embedded turn with no
  gateway (exit 0 ok, 1 error, 2 timeout).
* Hermes Agent v0.23.x: `hermes chat -q/--query-file - --format stream-json --yolo --in DIR -m MODEL
  --provider custom --base-url URL --api-key KEY`. Since v0.22 the CLI is a client of a per-`HERMES_HOME`
  gateway, so every DAASH job gets its own `HERMES_HOME`. It is an MCP client through `config.yaml`
  (`mcp_servers.<name>: {url, headers}`). An already-running Hermes is reached through its **API server**
  (`API_SERVER_ENABLED`, port 8642, Bearer `API_SERVER_KEY`; `/v1/runs` with `run_id`, `/events` (SSE),
  `/stop`; `/v1/responses`; session chosen with `X-Hermes-Session-Id`). Hermes does not speak A2A.

## 1. Scope

* **MCP server** (section 2): DAASH's tools for any MCP-capable agent, per-job identity, served by the
  coordinator.
* **Team messages** (section 3): `send_message` / `read_messages` for every agent, the native one included.
  Unread messages are put into the recipient's next prompt. This is the first step towards self-organizing teams
  (PLAN "After 0.3" item 2); routing does not change yet.
* **Runner agent types** (section 4): `openclaw` and `hermes` adapters, installed in the runner container and
  pointed at the local gateway. Every runner-launched CLI gets the DAASH MCP server for its job.
* **Connected agents** (section 5): a DAASH delivery slot can be an existing agent reached over the network:
  protocol `a2a` (OpenClaw and any other A2A 1.0 agent) or `hermes-api` (Hermes's API server). The engine talks
  to it directly. No runner is needed, and DAASH does not choose its model.
* **Agent-facing port** (section 6): the MCP and runner endpoints can be published to other machines without
  publishing the admin API.
* **DAASHboard** (section 7): connect an agent, generated config snippets for the agent's side, status, the
  delivery mix, messages in the activity feed, Help.
* Unchanged: judges, the coordinator, planning, prioritizing and classification stay native. Judging,
  thresholds, deterministic checks and command checks are as in 0.3.

## 2. The DAASH MCP server (`app/mcp_server.py`, `app/agent_tools.py`)

2.1 **Shared tools.** Move the engine tools out of `Jobs._engine_tools` into `app/agent_tools.py`:
`build_agent_tools(ctx, *, workspace: bool) -> list[Tool]`, where `ctx` carries the pool, repos, embedder, the
signal function and swarm/team/agent/task. The native loop and the MCP server use the same functions, so
behaviour cannot drift. Tools (names as now, plus the new ones):

| Tool | Args | Notes |
|---|---|---|
| `remember` | content | as 0.3 |
| `recall` | query? | as 0.3 |
| `create_subtask` | name, description?, artifact?, acceptance_criteria[], required_capabilities[] | as 0.3 |
| `propose_team` | name, mission_statement, reason? | as 0.3 |
| `vote_on_proposal` | proposal_id, approve | as 0.3 |
| `list_proposals` | — | new: pending proposals of the agent's team that it has not voted on |
| `team_roster` | — | new: the team's agents (name, role, agent type, capabilities, state, current task name) |
| `send_message` | to, text | new, section 3 |
| `read_messages` | — | new, section 3 |
| `current_task` | — | new: the task, artifact, acceptance criteria and latest feedback (helps agents that lose context) |
| `list_files`, `read_file`, `write_file`, `append_file`, `delete_file` | as the native tools | **only for connected agents** (`workspace=True`); they edit the live swarm workspace with the native tools' path rules and size limits |

`submit` stays native-only. A remote or connected agent submits by finishing (its final reply is the summary).

2.2 **Transport.** MCP streamable HTTP at `POST /mcp` (stateless; JSON responses; `GET /mcp` → 405), served by
the coordinator on the admin port and on the agent port (section 6). Use the official `mcp` Python SDK
(pinned) if it mounts cleanly in FastAPI. Otherwise write a small hand-rolled JSON-RPC handler covering
`initialize` (echo a supported `protocolVersion`), `notifications/initialized`, `ping`, `tools/list` and
`tools/call`. Tool errors return `isError: true` with the message, never an HTTP error.

2.3 **Identity: two kinds of bearer token** (`Authorization: Bearer ...`; missing or unknown → HTTP 401):
* **Job token** `djt_<32 url-safe random>`: minted when a runner leases a remote job. Only its SHA-256 is stored
  (`remote_jobs.mcp_token_hash`). It is returned once, in the lease (section 4.1), and is valid only while that job
  is `leased`. It maps to swarm/team/agent/task of the job.
* **Connected-agent token** `dca_<32 url-safe random>`: one per connected agent. It is set once in the agent's
  own MCP config, and only its hash is stored (`connected_agents.mcp_token_hash`). It maps to that agent's
  **active connected jobs**. Every tool accepts an optional `job` argument. Without it, the agent's single active
  job is used; with none or several, the call fails with a message listing the active job ids. The work prompt
  always states the job id.
* Every call re-checks that the job is still active and the swarm is ACTIVE. Otherwise: *"This DAASH job has
  ended (the swarm was paused, or the task moved on); stop working on it."* (A2A cannot cancel a running
  OpenClaw task, so this is how a cancelled agent is stopped from writing.)
* Each call records an event of kind `tool` (actor = the DAASH agent, detail = tool name and short args) and
  updates `connected_agents.mcp_seen_at` / the job's `payload.mcp_calls` count. DAASHboard can then show that an
  agent really uses its tools.

## 3. Team messages

* Table `agent_messages` (id, swarm_id, team_id, from_agent_id, to_agent_id NULL = whole team, text ≤ 4000
  chars, created_at, read_at NULL). For a whole-team message, read state is per recipient: store one row per
  recipient (fan-out on send) so `read_at` stays simple.
* `send_message(to, text)`: `to` is a team member's name (`Delivery-2`, case-insensitive), `team` (every other
  delivery agent of the team) or `coordinator` (the Swarm Coordinator; stored and shown, and read during vision
  review and planning). Unknown names fail with the list of valid names. Rate limit: 20 messages per agent per
  task attempt. Event `message`: *"Delivery-1 → Delivery-2: <first 200 chars>"*.
* `read_messages()`: unread messages to this agent, oldest first, marked read.
* Prompts: every delivery work prompt (native, remote, connected) gets a **"Messages from your team"** section
  with the agent's unread messages, which are then marked read, plus one line naming the messaging tools. The
  Swarm Coordinator's planning and vision-review prompts include messages sent to `coordinator`.

## 4. Runner agent types and MCP for runner-launched CLIs

4.1 **Lease.** The Job JSON (DESIGN-0.3 §5.2) gains `"mcp": {"path": "/mcp", "token": "djt_..."}`. The runner
builds the URL from its own coordinator URL, so it is reachable wherever the runner is. When a job ends (result,
cancel, expiry, abandon), its token stops working.

4.2 **MCP config per CLI** (written into the job's private config/state folder, never the user's own):
* `claude-code`, `openclaude`: `--mcp-config <file>` with
  `{"mcpServers": {"daash": {"type": "http", "url": URL, "headers": {"Authorization": "Bearer TOKEN"}}}}`.
  Tools must be allowed without prompts (e.g. `--allowedTools mcp__daash` or the permission mode already used).
* `codex`: `-c mcp_servers.daash.url=URL` with the token through `bearer_token_env_var` (or the form the
  installed Codex version supports; verify).
* `openclaw`: a per-job config (`OPENCLAW_CONFIG_PATH`/`OPENCLAW_STATE_DIR`, or what the installed version
  documents) with `mcp.servers.daash {url, transport: "streamable-http", headers}` and the gateway provider.
* `hermes`: a per-job `HERMES_HOME` whose `config.yaml` has `model: {provider: custom, base_url, default}`
  and `mcp_servers.daash {url, headers}`, with memory and skills writing turned off (DAASH memory is the
  shared one).
* `fake`: `DAASH_MCP_URL` / `DAASH_MCP_TOKEN` env vars. A scenario can call `remember` and `send_message` over
  MCP (for e2e).
* The remote work prompt adds: *"You have DAASH tools (MCP server `daash`): remember/recall for your persistent
  memory, team_roster, send_message/read_messages to coordinate with your team, create_subtask, propose_team,
  vote_on_proposal. Use them."*

4.3 **`openclaw` adapter** (capabilities `edit_files`, `run_code`): `openclaw agent exec --message-file -
--cwd <job dir> --json --model <provider>/<model> --timeout <s>`, with private state. DAASH's system prompt goes
first in the message (exec has no system-prompt flag), unless the version offers one. Parse the `--json`
envelope (final text, usage when present). Exit 2 = failed (timeout). Model/provider/network errors = `error`.

4.4 **`hermes` adapter** (capabilities `edit_files`, `run_code`): `hermes chat --query-file - --format
stream-json --yolo --in <job dir> -m <model> --max-turns N`, private `HERMES_HOME` per job, with a terminal
backend of `local`, since the runner container is the sandbox. Parse the JSONL `result` record (exit code 3 =
`error`, gateway owner lost). After the job, stop that `HERMES_HOME`'s gateway (`hermes gateway stop` or kill
its process group) so nothing lingers.

4.5 **Runner container** (`Dockerfile.runner`): install OpenClaw (npm, pinned) and Hermes Agent (pinned; the
install script or a copy from the official image), both runnable as the runner user. `runner/runner.docker.json`
gains `openclaw` and `hermes` on the local gateway (models `instant fast great amazing`).
`KNOWN_HARNESSES`, `HARNESS_CAPABILITIES`, `HARNESS_LABELS` and `LISTED_HARNESSES` gain `openclaw` ("OpenClaw")
and `hermes` ("Hermes Agent").

## 5. Connected agents (existing agents reached over the network)

5.1 **Data** (table `connected_agents`): id, name (unique, shown in DAASHboard and to the team: e.g. "Jane"),
protocol (`a2a` | `hermes-api`), url (what the user entered: an Agent Card URL or gateway origin for A2A, the
API base URL for Hermes), endpoint (A2A JSON-RPC URL; see 5.3), skill (A2A skill/agent id, informational), token
(the bearer DAASH sends; write-only in the API, never returned or logged), mcp_token_hash, capabilities
(default `["edit_files"]`), max_jobs (default 1), enabled, card (JSON, A2A), last_ok_at, last_error,
mcp_seen_at, created_at.

5.2 **Swarm config.** `HarnessSpec` gains `connected_agent: Optional[str]` (connected agent id) with
`harness = "connected"`. The model is ignored (shown as "its own model"), and capabilities default to the
connected agent's. `validate_config`: a `connected` row must name an existing, enabled connected agent. One
connected agent may fill several rows. Its `max_jobs` caps concurrent work across them.

5.3 **A2A client** (`app/connectors.py`, httpx, no new heavy dependency):
* *Connect*: GET the Agent Card from the entered URL (or `<origin>/.well-known/agent-card.json`). From the card's
  JSON-RPC interface, keep the **path**, but use the **origin the user entered**: advertised hosts are often LAN
  addresses that DAASH's containers cannot reach. The user can override the endpoint.
* *Check* (cheap, spends no model tokens): `GetTask` with id `daash-probe`. "Task not found" (or any JSON-RPC
  error that is not an auth error) = reachable and the token is accepted. HTTP 401/403 = token rejected.
* *Send*: `SendMessage` with `message: {messageId, role: "ROLE_USER", contextId:
  "daash-<swarm8>-<task8>-a<attempt>", parts: [{text}]}` and `configuration: {returnImmediately: true}`.
  Store the returned task id in the job. Poll `GetTask` every `connected_poll_seconds` (default 10, which keeps
  well under 30 requests a minute). `TASK_STATE_COMPLETED` → the reply is the text of the artifacts (or of the
  final message). `FAILED`/`REJECTED` → attempt failed with the agent's text. A task that is unknown after it was
  sent (gateway restarted) → infrastructure error: resend as a new job, no attempt counted.
* Message size: the prompt must fit in 60 000 bytes. Trim the workspace listing and excerpts first, then
  feedback history. Never cut the task, its criteria or the rules.

5.4 **Hermes API client** (`hermes-api`): `POST /v1/runs` with the prompt (and
`session_id = daash-<swarm8>-<task8>-a<attempt>`), then follow the run (events or polling, whichever the
installed version offers; verify against a real Hermes container). Cancel with `/stop`. `GET /health` and an
authenticated `GET /v1/models` are the cheap check.

5.5 **Engine** (`Jobs._connected_work`, alongside `_remote_work`):
1. Same preamble as remote work. The job is a `remote_jobs` row with `harness="connected"`, `runner_id` NULL,
   `payload = {connected_agent_id, protocol, external_id, context_id}`. `ensure_active` re-attaches after an engine
   restart. These rows are never leased by runners and are skipped by `requeue_expired`.
2. Prompt = `prompts.connected_work_prompt(...)`. A2A has no system channel, so it starts with the agent's
   composed system prompt, then the task, criteria, feedback, workspace listing, team messages and job id, and
   these rules:
   *"Use the DAASH tools (MCP server `daash`) to read and write the shared workspace: list_files, read_file,
   write_file (pass job=<id>). If you do not have DAASH tools, put every file you create or change in your reply
   as `<<<FILE path>>>` … `<<<END FILE>>>` blocks, and `<<<DELETE path>>>` for deletions. Work only on this
   task. Finish with a short summary of what you did and how each criterion is met."*
3. Wait for completion, bounded by `remote_job_timeout_minutes`. Then apply any reply file blocks with the
   native path rules and limits. Files written through MCP are already in the workspace. The deliverable's
   `files` = paths written through MCP during this job ∪ paths from reply blocks.
4. Review exactly as remote work (deliverable `harness = "connected"`, `connected_agent = name`). Event:
   *"Delivery-2 (Jane, via A2A) submitted ..."*.
5. Cancel (pause/verify): mark the job cancelled. `hermes-api` → `/stop`. `a2a` cannot cancel: the MCP server
   then refuses that job's calls (2.3), and a late reply is ignored.
6. Infrastructure errors (unreachable, 5xx, rate-limited, auth failure) → `RemoteError`: cooldown, no attempt.
   `last_error` is set and shown in DAASHboard. After `remote_fallback_minutes` of unreachability, the attempt
   falls back to the built-in agent, with an event (as 0.3 does for runners).

5.6 **Escalation means handing the work over.** DAASH does not pick a connected agent's model, so the model
ladder does not apply to it. When a connected agent's attempt fails (rework), the task is reassigned to the least
recently active **other** capable delivery agent of the team, if one exists and is idle. Otherwise the same agent
tries again. Event: *"Jane could not finish '<task>'; handing it to Delivery-3."*

5.7 **Availability** (`/availability`, Agents & models): each connected agent appears with its protocol, status
(`available` = last check ok; `unreachable`; `token_rejected`; `disabled`), capabilities, "DAASH tools: in use
(last call …) / not yet used", and whether this swarm allows it. A check runs on demand ("Test") and when the
Agents page loads, at most once a minute per agent.

## 6. Agent-facing port

A second listener serves only `/health`, `/mcp` and `/runner/*`: compose service `agent-api`, the same image,
`uvicorn app.agent_api:app --port 8001`, published at `${DAASH_AGENT_BIND_ADDR:-127.0.0.1}:8001:8001`. Runner
and MCP routes move into `APIRouter`s that both apps include. The admin port (8000) keeps
`DAASH_BIND_ADDR`/`DAASH_API_TOKEN` as in 0.3. `DAASH_AGENT_PUBLIC_URL` (e.g.
`http://m5m128gb.tail391a32.ts.net:8001`) is the address shown in snippets for connected agents and remote runners.
When it is unset, DAASHboard says so and shows how to set it. Nothing on the agent port reaches admin routes.

## 7. DAASHboard

* **Agents & models**:
  * The agent types table gains OpenClaw and Hermes Agent.
  * New **Connected agents** panel: name, protocol, address, status, capabilities, DAASH tools in use, last
    error, plus Test, Disable/Enable and Remove.
  * **Connect an agent** form:
    1. Name, protocol, URL, capabilities and max jobs. For A2A, DAASH fetches the card and shows its name and
       skills.
    2. The token. For A2A, DAASH **generates** the token the agent will accept from DAASH and shows the exact
       `channels.a2a.peers.daash` snippet to add on the agent's side. For Hermes, the user pastes the API server key.
    3. **Give it DAASH's tools**: DAASH generates the agent's MCP token and shows the `mcp.servers.daash`
       snippet (OpenClaw) or the `mcp_servers.daash` snippet (Hermes) with `DAASH_AGENT_PUBLIC_URL`. Tokens are
       shown once.
    4. "Test connection".
* **Create a swarm / Settings defaults**: delivery rows can pick a connected agent ("Jane (A2A)"). The model
  select reads "its own model".
* **Swarm**: roster badges ("Jane · A2A"), agent messages in the activity feed, and the tool calls of external
  agents visible in the agent's details.
* **Help**: a second Help topic, "Connecting agents", kept in the repo and served like the prompts guide. It
  covers what connected agents are, the A2A and Hermes steps, the snippets, DAASH tools, networking
  (`DAASH_AGENT_BIND_ADDR`, Tailscale) and limitations.

## 8. Tests

* **Unit**:
  * The MCP handler (initialize, list and call, errors, token kinds, the job-ended refusal).
  * Agent tools through the shared module, and message fan-out, limits and prompt inclusion.
  * Adapters: argv/env/config for openclaw and hermes, and the MCP config for every CLI.
  * The A2A and Hermes clients against stub servers: card parsing, endpoint origin rule, check, send/poll,
    errors.
  * Reply file-block parsing (paths, limits, malformed blocks).
  * Escalation by handover, and config validation.
* **Integration**:
  * `/mcp` over HTTP with the real database: a job token can remember and recall, and is refused after the job
    ends.
  * A connected job driven to Review against an in-process fake A2A agent that calls `read_file`/`write_file`
    over MCP. The same with a fake that answers with file blocks.
  * The agent-api app serves only its routes.
* **E2E**:
  * A fake A2A agent container (scripted, no LLM) joins a swarm with the fake LLM. It uses DAASH MCP tools,
    including `remember` and `send_message`, and its task reaches Done.
  * The fake CLI on a runner calls `remember` over MCP.

## 9. Live verification (after the tests pass)

1. Runner container: an `openclaw` and a `hermes` delivery agent on the local gateway, with DAASH memory being
   written (`remember` rows from external agents) and messages exchanged.
2. A test OpenClaw gateway in Docker with the A2A channel and DAASH's MCP server, connected as an agent.
3. The user's **Jane** (A2A, janes-mac-mini) and **Fitz** (A2A, m3u512gb). These need the user's agreement
   and changes on those machines:
   * a `daash` peer token and the `daash` MCP server in each agent's OpenClaw config
   * DAASH's agent port reachable over Tailscale
   * for Fitz, its gateway reachable from other machines (on 2026-11-11 it answered only on 127.0.0.1; the macOS
     firewall is on)
