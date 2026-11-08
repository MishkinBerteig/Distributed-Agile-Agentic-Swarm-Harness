# DAASH 0.1 "Prototype" — Design Contract

This is the binding design for the 0.1 prototype. README.md states the intent;
this document turns it into concrete modules. `app/models.py` is the shared
domain contract. Do not change it casually: if a module truly needs a new
field, add it there, in the migration and in the repo, and say so in your
report.

## 1. What changed from the earlier slices (and why)

The earlier slices drifted from the README in several places. 0.1 corrects them:

| Earlier work | README intent | 0.1 |
|---|---|---|
| Mission Judgement = embedding cosine score | Mission Judgement is an **agent** | LLM agent verdicts with confidence; cosine endpoint removed |
| DoR/DoD = free text from an LLM | DoR is a **commitment policy** with hard minimum rules; DoD has **vision + mission levels** and numeric thresholds | Deterministic DoR gate + DoD evaluator in `app/policy.py`; LLM text only *adds* team-specific criteria |
| Agents = long-lived OpenClaude CLI sessions through a host runner (never worked end-to-end; the runner crashed on every spawn) | Containerised harness, heterogeneous LLMs | Native **"daash" harness**: an OpenAI-compatible tool-calling loop inside the containers, served by the m3u512gb gateway (`instant`, `fast`, `great`, `amazing`). `app/runner.py` is kept as an *experimental* future external-CLI harness and is not wired in |
| No Swarm Coordinator, no swarm config | Coordinator agent + allowed harness/provider/model lists + resource limits + global Values & Principles | `SwarmConfig`, `GlobalConfig`, coordinator bootstrap job, budget enforcement |
| Single free-text acceptance criteria | Several criteria, each **green/red with certainty** | `acceptance_criteria` table; deterministic check kinds + rubric |
| No cycle-time data | p90 cycle time, error rates, team failure rates | `committed_at`/`done_at`, `task_events`, `agent_calls`, `app/metrics.py` |
| No team creation by vote; mission not tied to creator | Delivery agents vote (majority) to spin out teams; mission must refer to creator's vision/mission | `team_proposals` + `creator_kind`/`creator_team_id` |
| No learning | 3 learning mechanisms | Patterns (learnings table, injected in prompts), routing (model escalation + success-rate learning); automation scripts recorded only (execution deferred) |

## 2. Runtime architecture

```
 dashboard (Vite/React :7173) --/api--> coordinator (FastAPI :8000) --+--> PostgreSQL+pgvector (source of record)
                                                                      +--> Redis decision bus (ephemeral signals)
 engine (python -m app.engine) -------------------------------------->+   same DB/bus; drives the agents
     \--> LLM gateway (OpenAI-compatible; DAASH_LLM_ENDPOINT, DAASH_LLM_API_KEY)
     \--> /workspaces/<swarm_id>/  (named volume shared with coordinator: the artifacts)
```

* **coordinator**: REST API only (no agent work); runs migrations at start.
* **engine**: one async process. Every `DAASH_ENGINE_TICK_SECONDS` (default 3) it loads a
  `SwarmSnapshot` for the live swarm, calls the **pure** `decide(snapshot) -> list[Action]`,
  then executes actions. LLM actions run as background asyncio jobs (bounded by
  `limits.max_simultaneous_agents`) keyed by `Action.job_key()` so a job never runs twice.
  On start-up it resets every BUSY agent to IDLE (nothing is running after a restart);
  In-Progress/Review tasks are simply picked up again: the work is idempotent and the workspace persists.
* Only one live (non-ARCHIVED) swarm at a time (kept from earlier slices).

## 3. Modules and ownership

| Module | Purity | Responsibility |
|---|---|---|
| `app/models.py` | pure | domain + API types (contract) |
| `app/config.py` | pure | `Settings` (env, prefix `DAASH_`) |
| `app/policy.py` | pure | lifecycle transitions, task transitions, DoR gate, DoD evaluation, p90, voting, limits |
| `app/routing.py` | pure | model choice per role/complexity/attempt + learned success rates |
| `app/metrics.py` | pure | `compute_metrics(...)` for the dashboard |
| `app/checks.py` | fs only | deterministic acceptance-criterion checks in a workspace dir |
| `app/llm.py` | http | OpenAI-compatible client, tool loop, tolerant JSON extraction |
| `app/tools.py` | fs only | `Tool`, `ToolRegistry`, safe workspace file tools |
| `app/prompts.py` | pure | system-prompt hierarchy, job prompts, output parsers/validators |
| `app/repo.py` | db | asyncpg repositories + `load_snapshot()` |
| `app/bus.py` | redis | decision bus (unchanged from Slice 2) |
| `app/embeddings.py` | pure | unchanged; used for learnings/memory retrieval |
| `app/engine.py` | orchestration | `decide()` (pure) + `Engine` executor + jobs |
| `app/main.py` | http | FastAPI endpoints (section 7) |
| `app/runner.py` | stdlib | experimental, not wired (bug-fixed only) |
| `dashboard/` | ui | DAASHboard |

## 4. Settings (`app/config.py`, env prefix `DAASH_`)

`DATABASE_URL`, `REDIS_URL`, `BUS_TTL_SECONDS`, `BUS_SIGNALS_MAX`, `EMBEDDING_MODEL`,
`EMBEDDING_USE_MODEL`, `LLM_ENDPOINT` (default `http://m3u512gb.tail391a32.ts.net:12345`;
`m3u512gb.local` is mDNS and does not resolve inside Docker), `LLM_API_KEY` (from `.env`,
never committed), `LLM_TIMEOUT_SECONDS` (600), `LLM_MAX_TOKENS` (16384; these are reasoning
models, so small budgets return only `reasoning_content`), `WORKSPACE_ROOT` (`/workspaces`),
`ENGINE_TICK_SECONDS` (3), `ENGINE_ENABLED` (true).

## 5. Data model (migration `e5a1d0c0f001_prototype_0_1`)

Dev databases are disposable. The migration **drops** the old tables (`agent_sessions`,
`memory_entries`, `transcripts`, `tasks`, `teams`, `swarms`) and creates the schema
below. Column names = `app/models.py` field names. `jsonb` for dicts and lists;
`text[]` for `depends_on`; timestamps `timestamptz`.

* `global_config(key text pk, value jsonb)`. Keys are `values_principles` and `default_swarm_config`.
* `swarms` (+ `config jsonb`, `definition_of_done`, `paused_reason`, `retrospective_done bool`, `started_at`).
  Partial unique index `uq_one_live_swarm` on `((1)) WHERE lifecycle_state NOT IN ('ARCHIVED','DELETED')`.
* `teams` (FK swarm ON DELETE CASCADE; `creator_team_id` FK teams ON DELETE SET NULL).
* `agents` (FK swarm/team CASCADE).
* `tasks` (FK swarm/team CASCADE, `parent_id` FK tasks ON DELETE CASCADE, `embedding vector(768)`,
  `deliverable jsonb default '{}'`). Indexes on `(swarm_id, status)` and `team_id`.
* `acceptance_criteria` (FK task CASCADE, unique `(task_id, ordinal)`).
* `verdicts` (FK task CASCADE).
* `task_events` (`id bigserial`; FK swarm CASCADE; `task_id`/`team_id` nullable and **not** FK, so
  history survives task deletes). Index `(swarm_id, created_at desc)`.
* `agent_calls` (FK swarm CASCADE; `transcript jsonb`). Index `(swarm_id, created_at)`.
* `learnings` (`swarm_id` nullable FK CASCADE; `embedding vector(768)`).
* `memories` (FK agent CASCADE; `embedding vector(768)`).
* `team_proposals` (FK swarm CASCADE; `votes jsonb`).
* `feedback` (FK swarm CASCADE).

`app/orm.py` mirrors this for Alembic metadata only. Keep it in sync, or delete it and set
`target_metadata = None` in `migrations/env.py`. 0.1 does the latter: hand-written migrations, less drift.

## 6. Domain rules (pure; `app/policy.py`, `app/routing.py`, `app/metrics.py`)

### 6.1 Task status transitions
```
Pending -> Ready | Rejected
Ready -> In-Progress | Pending | Rejected
In-Progress -> Review | In-Progress (rework) | Failed | Pending (team restart -> Rejected)
Review -> Done | In-Progress (rework) | Failed
Done, Failed, Rejected -> (terminal; user may reopen to Pending via API)
```
Also: `can_transition_task(frm, to) -> bool`. Anything not listed (including any user override) goes through the API's explicit `PATCH`, which may also set Pending.

### 6.2 Definition of Ready (commitment policy), README minimums, all required
1. at least one Mission Delivery agent of the team is IDLE with no In-Progress task assigned;
2. the team has **no In-Progress task older than the trailing p90 cycle time** of the last 100 Done
   tasks swarm-wide (`now - committed_at > p90`). No p90 yet (fewer than 5 Done tasks) means no constraint;
3. Mission Judgement approved the task as the next highest priority: status Ready, and the lowest
   `priority` among the team's Ready tasks whose `depends_on` are all Done;
4. task has at least one acceptance criterion and all `depends_on` tasks are Done.

`dor_check(task, team_tasks, team_agents, p90_seconds, now) -> DoRResult(ok: bool, reasons: list[str])`
and `next_ready_task(team_tasks) -> Task | None`.

### 6.3 Definition of Done
Vision level (`swarm.definition_of_done`) + team-mission level (`team.definition_of_done`) are both
shown to the judges. Pass requires **all** of the following:
* quality: every deterministic check passes (score 1.0 each, mean >= `thresholds.deterministic`),
  and every rubric criterion's QJ score is `>= thresholds.rubric`. No deterministic criteria means
  the deterministic part passes vacuously. No rubric criteria means the rubric part passes vacuously.
  QJ's overall `passed` must also be true;
* mission: MJ `expected_value_met` is true and `confidence >= thresholds.mission_confidence`.

`evaluate_dod(deterministic: list[bool], rubric_scores: list[float], qj_passed: bool, mj_met: bool,
mj_confidence: float, thresholds) -> DoDResult(passed, quality_passed, mission_passed, quality_score, reasons)`.
`quality_score` = mean of all criterion scores (deterministic counted 1/0).

On failure: `attempts += 1`. Then `attempts >= max_attempts_per_task` means **Failed**. Otherwise **In-Progress**
(rework, same agent) with `task.feedback` = combined judge feedback. Criteria get green/red + evidence.

### 6.4 Voting
`majority_quorum(n) = n // 2 + 1`. `tally(votes, n_voters) -> "approved"|"rejected"|"pending"`: approved
once approvals >= quorum; rejected once rejections make approval impossible.

### 6.5 Limits
`limits_exceeded(swarm, tokens_used, cost_usd, now) -> list[str]` covers wall-clock since `started_at`, total
tokens, and USD budget. Any reason means the engine pauses the swarm and records `paused_reason`. A user resume
while still over the limit gets paused again on the next tick. The pause reason tells the user to raise the limit.

### 6.6 Routing (learning mechanism 3)
`choose_model(role, config, complexity, attempts, stats) -> str`:
* judges and coordinator use their configured model (`role_models` / `coordinator.model`);
* delivery: start index on `model_ladder ∩ allowed_models` = position of `role_models.mission_delivery`
  for simple/moderate (unknown = moderate), +1 for complex. Then +1 per prior failed attempt, capped at the top.
* learned bump: if `stats[(model, complexity)]` has >= 5 samples and success rate < 0.5, start one tier higher.
  `stats` comes from Done/Failed outcomes (`routing_stats(tasks, verdict_models)`).

### 6.7 Metrics
`compute_metrics(tasks, events, calls, teams, now, interval_seconds=3600) -> dict`:
* `p90_cycle_seconds`: nearest-rank p90 of `done_at - committed_at` over the last 100 Done tasks;
* `cycle_times`: the last 20 `{task_id, name, seconds}`;
* `error_rate`: `{interval_seconds, series: [{start, errors, calls, rate}]}` over the last 24 buckets. Errors
  are failed `agent_calls` plus `rework`/`failed`/`error` events;
* `team_failure_rates`: per team `{team_id, team_name, tasks_committed, failed_attempts, tasks_failed,
  failures_per_task}`, where `failures_per_task = failed review attempts / tasks committed`;
* `tasks_by_status`, `tokens: {total, by_model, by_role}`, `cost_usd`.

## 7. Engine (`app/engine.py`)

### 7.1 `decide(snap: SwarmSnapshot) -> list[Action]` (pure, unit-tested)
`running(k)` means `k in snap.running_jobs`. Never emit an action whose job key is running.
```
if swarm.state == LEARNING and not retrospective_done: [RETROSPECTIVE]
if swarm.state != ACTIVE: []
if limits_exceeded: [PAUSE_FOR_LIMITS(detail=reasons)]
if swarm.definition_of_done is None or no teams: [BOOTSTRAP]        # nothing else until bootstrapped
for each APPROVED proposal with no created_team_id: MATERIALIZE_TEAM
for team in teams with status active:
    if team.definition_of_done is None: GENERATE_STANDARDS; continue
    for t in team tasks with status Review: REVIEW_TASK(t)
    for t in In-Progress: WORK_TASK(t, agent=t.assigned_agent_id)
    for each idle delivery agent without an In-Progress task (one commit per agent per tick):
        c = next_ready_task(); if c and dor_check ok: COMMIT_TASK(c, agent)
    if no Ready task and some Pending task (deps Done or none): PRIORITIZE(team)
    if team has no open tasks:
        if lifetime task count >= max_tasks_per_team: FAIL_TEAM
        elif team has Done/Failed tasks and (last_assessed_at is None or < latest terminal task time): ASSESS_MISSION
        else: PLAN_TASKS (uses team.next_focus)
if teams and every team is accomplished/failed: [COMPLETE_SWARM]
```

### 7.2 Jobs (executor). Each LLM job writes one `agent_calls` row and updates agent counters/state.
* **BOOTSTRAP** (coordinator, `coordinator.model`): returns vision-level DoD, and, when the swarm has no
  teams, 1..`max_teams` teams `{name, mission}`. Each mission must explicitly reference the Vision. Creates
  teams (`creator_kind=swarm_coordinator`) plus agents.
* **Agents per team** (created whenever a team is created): 1 MJ, 1 QJ, `delivery_agents_per_team` delivery
  agents named `Mission Judgement`, `Quality Judgement`, `Delivery-1..n`. One coordinator agent per swarm.
* **GENERATE_STANDARDS** (QJ): team DoR additions + team DoD (`extract_quality_json`, honour guidance).
* **PLAN_TASKS** (delivery agent #1): JSON list of up to `max_new_tasks_per_plan` tasks
  `{name, description, artifact, acceptance_criteria: [{text, check_kind, check_path, check_arg}], depends_on: [indexes]}`.
  Validated by `prompts.parse_plan`. Invalid check specs are downgraded to rubric. Tasks are created
  Pending with `created_by` = agent id.
* **PRIORITIZE** (MJ): sees Pending + Ready tasks, team DoR, done work. Returns
  `{order: [task_id...], next_task_id, rejections: [{task_id, reason}]}`. Engine sets `priority` by
  order, rejects listed tasks (Rejected + reason), moves `next_task_id` to Ready with `approved_at`.
* **COMMIT_TASK** (deterministic): re-check DoR then In-Progress, `assigned_agent_id`, `committed_at`.
  Event `committed`.
* **WORK_TASK** (delivery): if `complexity` is None, classify first (classifier model, `parse_classification`).
  Model = `choose_model`. Tool loop with workspace tools + engine tools:
  `create_subtask`, `remember`, `recall`, `propose_team`, `vote_on_proposal` (only if pending proposals),
  `submit(summary)` (terminal). The prompt carries the hierarchy, the task + criteria, and the last
  judge feedback, plus the existing workspace listing. Submit sets status Review, `deliverable = {summary, files
  written this attempt}`. If no submit happens within `max_tool_steps`, or on an LLM error, the attempt
  counts as failed (attempts+1, rework or Failed) and an error event is written.
* **REVIEW_TASK**: run deterministic checks (`app/checks.py`), then QJ (rubric criteria plus overall
  quality, with deliverable file contents truncated to about 12k characters each) and MJ (expected value
  plus confidence). Store 2 verdicts, update criteria, `evaluate_dod`, transition. Publish
  `verdict.quality`, `verdict.mission`, `task.done` / `task.rework` bus signals. On Done, record the routing
  outcome. Then ask nothing more: a pattern learning is extracted only on Failed or after a rework (QJ feedback
  is stored as a `pattern` learning when a task needed more than one attempt).
* **ASSESS_MISSION** (MJ): `{status: continue|accomplished|restart, confidence, reasoning, next_focus}`.
  `accomplished` needs `confidence >= mission_confidence`; otherwise it is treated as continue. `restart` means:
  open tasks are set to Rejected ("team restart"), `generation += 1`, a learning is recorded, and `next_focus` is set.
  Always set `last_assessed_at = now`.
* **MATERIALIZE_TEAM** / **FAIL_TEAM** / **COMPLETE_SWARM** / **PAUSE_FOR_LIMITS**: deterministic DB updates
  plus an event and a bus signal. COMPLETE_SWARM sets the swarm to VERIFICATION.
* **RETROSPECTIVE** (coordinator): swarm history in, `{learnings: [{kind, content}]}` out, stored with
  `swarm_id = NULL` (global, injected into future swarms). Sets `retrospective_done`.

### 7.3 System-prompt hierarchy (deterministic, every call; `prompts.compose_system_prompt`)
```
# Vision                      (swarm, verbatim)
# Values and Principles       (global; omitted if empty)
# Mission                     (team, verbatim; omitted for coordinator)
# Definition of Done          (vision level, then team-mission level, if known)
# Definition of Ready         (team additions; MJ/QJ/delivery)
# Your Role: <role>           (role job description)
# User Feedback               (swarm feedback, newest last; omitted if none)
# Learnings                   (up to 8 relevant: swarm + global)
# Your Memory                 (up to 8 of this agent's memories)
```
Job instructions and output contracts go in the **user** message, never the system prompt, so the
system prompt is identical for every call an agent makes in a given state.

## 8. LLM harness (`app/llm.py`, `app/tools.py`)

* `LLMClient(endpoint, api_key, timeout, max_tokens, transport=None, max_retries=2)`:
  `list_models()`, `chat(model, messages, tools=None, temperature=0.3, max_tokens=None) -> ChatResult`,
  `run_tool_loop(model, system, user, registry, max_steps) -> LoopResult`,
  `complete_json(model, system, user, parse, retries=1) -> (parsed, LoopResult-like usage/transcript)`.
  When parsing fails, `complete_json` re-asks once with the parse error appended.
* Gateway facts (verified): OpenAI-compatible `/v1/chat/completions`, Bearer auth, tool calling works
  (`finish_reason: tool_calls`). Models are **reasoning models**: `message.content` may be `None`/missing
  while `reasoning_content` is present. Treat missing content as `""`, never as a crash. A small
  `max_tokens` yields `finish_reason: length` with no content. `usage` has `prompt_tokens`,
  `completion_tokens`, `total_tokens`.
* Retries: on connection errors, timeouts, and HTTP 429/5xx (exponential backoff 1s, 2s). Not on 4xx.
* Tool loop: append assistant message (with `tool_calls`), execute each call, and append
  `{"role":"tool","tool_call_id","content"}`. Stop when a terminal tool runs, when the model answers with
  no tool calls (for a work task, nudge once: "call submit when done"), or at `max_steps`.
* `extract_json(text)`: strips `<think>…</think>`, code fences, surrounding prose. Returns the first
  balanced JSON object/array, or raises `ValueError`.
* `tools.py`: `Tool(name, description, parameters, handler, terminal=False)`,
  `ToolRegistry.specs()` (OpenAI format), `await registry.call(name, arguments_json) -> ToolOutcome(output,
  terminal, error)`. Tool errors are returned to the model as text and never raised. `safe_path(root, rel)`
  rejects absolute paths, `..` escapes, and symlinks resolving outside root. Workspace tools: `list_files(path)`,
  `read_file(path)` (truncate 20k chars), `write_file(path, content)` (creates dirs; records path in
  `registry.files_written`), `append_file`, `delete_file`. Max file size 1 MB.

## 9. HTTP API (`app/main.py`), JSON throughout

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | `{status:"ok"}` |
| GET/PUT | `/config` | `GlobalConfig` |
| GET | `/models` | `{models:[...], endpoint, reachable}` from gateway `/v1/models` (5s timeout, never 500) |
| GET | `/swarms` | `{swarms:[Swarm]}` |
| POST | `/swarms` | `SwarmCreate` → 201 `Swarm` + `teams` + `agents`; 409 if a live swarm exists; creates workspace dir |
| GET | `/swarms/active` | `Swarm` or `null` |
| GET | `/swarms/{id}` | `Swarm` + `teams` + `agents` |
| PATCH | `/swarms/{id}` | `SwarmUpdate` (vision only while CREATED → else 409) |
| POST | `/swarms/{id}/transitions/{action}` | 400 on illegal; `start` sets `started_at` if null; `resume` clears `paused_reason`; returns Swarm |
| POST | `/swarms/{id}/feedback` | `FeedbackCreate`; stores + transitions PAUSED→USER_FEEDBACK (if PAUSED); 201 |
| GET | `/swarms/{id}/feedback` | `{feedback:[Feedback]}` |
| DELETE | `/swarms/{id}` | only CREATED/VERIFICATION/ARCHIVED(else 409); cascades; removes workspace dir |
| GET | `/swarms/{id}/board` | `{swarm, teams:[{team, agents:[Agent], tasks:[Task+criteria]}], coordinator: Agent|null}`, the dashboard's main poll |
| GET | `/swarms/{id}/metrics` | `compute_metrics(...)` + `limits:{tokens_used,max_total_tokens,cost_usd,max_budget_usd,elapsed_minutes,max_wall_clock_minutes}` |
| GET | `/swarms/{id}/events?limit=100&team_id=&task_id=` | `{events:[TaskEvent]}` newest first |
| GET | `/swarms/{id}/artifacts` | `{files:[{path,size,modified}]}` |
| GET | `/swarms/{id}/artifacts/{path:path}` | `{path, content, truncated}` (safe_path; 404) |
| GET | `/swarms/{id}/learnings` | `{learnings:[Learning]}` swarm + global |
| GET | `/swarms/{id}/proposals` | `{proposals:[TeamProposal]}` |
| GET | `/agents/{id}/calls?limit=20` | `{calls:[AgentCall]}` newest first |
| GET | `/teams?swarm_id=` / POST `/teams` | POST creates team (`creator_kind=user`) + its agents |
| GET/PATCH/DELETE | `/teams/{id}` | |
| GET | `/teams/{id}/quality` | `{team_id, definition_of_ready, definition_of_done}` |
| POST | `/teams/{id}/quality` | `QualityGenerateRequest`; runs QJ synchronously via LLMClient; 502 on LLM/parse error |
| GET | `/tasks?swarm_id=&team_id=&status=` | `{tasks:[Task]}` with criteria |
| POST | `/tasks` | `TaskCreate` → 201 Task (Pending, `created_by:"user"`, swarm_id from team) |
| GET | `/tasks/{id}` | Task + `verdicts` + `events` + `children` |
| PATCH/DELETE | `/tasks/{id}` | user overrides are recorded as events with actor "user" |
| GET/POST | `/decisions`, `/proposals`, `/proposals/{id}`, `/proposals/{id}/votes` | decision bus, unchanged |

## 10. DAASHboard (dashboard/)

A single page that polls `/api/swarms/active`, `/board`, `/metrics` and `/events` every 3s while a swarm is live.
* **No live swarm**: create-swarm form with name, vision, optional vision DoD, optional teams (rows of
  name + mission; empty means the coordinator bootstraps), and collapsible config (role models from `/models`,
  delivery agents per team, limits). Below it: global Values & Principles editor and the archived swarms list.
* **Live swarm**: header with name, lifecycle badge, action buttons from `SWARM_ACTIONS`, feedback textbox in
  PAUSED, `paused_reason` banner, budget bars (tokens, wall clock). Vision and vision-DoD shown.
* **Metrics strip**: p90 cycle time, tasks by status, error-rate sparkline (inline SVG), team failure rates,
  tokens by model.
* **Teams** side by side, each with mission, creator, status, generation, and a roster (role, name, model,
  state, current task, tokens). DoR/DoD expand/regenerate with guidance. Kanban columns are Pending, Ready,
  In-Progress, Review, then Done/Failed/Rejected collapsed.
* **Task drawer** (click a card): description, artifact, criteria with green/red/unknown chips plus evidence,
  verdicts with scores and reasoning, event timeline, deliverable summary and files (open in artifact viewer),
  and user controls (reopen, reject, delete).
* **Activity feed** (events, human readable) plus bus signals, **Artifacts** browser (tree plus viewer),
  **Learnings**, **Agent calls** (click an agent to see recent transcripts).

## 11. Testing policy for 0.1

Only **fast, isolated unit tests** for basic logic: pure functions, parsers, the tool sandbox (with
`tmp_path`), the LLM client against `httpx.MockTransport`, and `decide()` on hand-built snapshots. No DB, no
Redis, no network, no Docker-in-tests. They live in `tests/unit/` and run with
`docker compose --profile test run --rm --build test` (no DB dependency). The earlier DB/Redis-backed tests
move to `tests/integration/` and are **not** run by default. Revive them after 0.1 together with E2E,
resilience and performance tests.
