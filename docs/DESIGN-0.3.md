# DAASH 0.3 — Distributed, heterogeneous harnesses (design contract)

0.3 lets a team's **Mission Delivery agents run on external agent CLIs** — OpenClaude, Claude Code, Codex —
on any device, through a pull-based **runner**, alongside the native `daash` harness. It adds
**authentication** for runners and (optionally) for the admin API and DAASHboard.
Everything in docs/DESIGN-0.1.md and docs/DESIGN-0.2.md still holds unless changed here.

Why: README asks for "teams of heterogeneous AI agents ... possibly with heterogeneous agentic harnesses and
underlying LLMs". The external CLIs also bring code execution and web research (PLAN "After 0.2" items 2–3).

## 1. Scope

* Remote harnesses apply to **delivery work only** (`WORK_TASK`). Judges, the coordinator, planning,
  prioritizing and classification stay native: they need strict JSON contracts and are cheap.
* Judging is unchanged: remote work is reviewed by the same deterministic checks, Quality Judgement and
  Mission Judgement, with the same thresholds.
* Pull model: runners dial out to the coordinator (works behind NAT/Tailscale); the coordinator never
  connects to a runner. The old push-style `app/runner.py` is removed (replaced by `daash_runner/`).

Harness names: `daash` (native), `openclaude`, `claude-code`, `codex`, `fake` (scripted test CLI).

## 2. Configuration (app/models.py)

```python
class HarnessSpec(BaseModel):          # unchanged fields; model "" = use routing (ladder)
    harness: str = "daash"
    provider: str = "default"
    model: str = "amazing"

class SwarmConfig(BaseModel):
    ...
    # Harness of each delivery agent, cycled: Delivery-i gets delivery_harnesses[(i-1) % len].
    # Empty (default) = every delivery agent is native "daash" (0.2 behaviour).
    delivery_harnesses: list[HarnessSpec] = []
    # A remote job no runner has leased after this many minutes runs natively instead
    # (event explains why). None = wait for a runner forever.
    remote_fallback_minutes: Optional[int] = 10

class Limits(BaseModel):
    ...
    remote_job_timeout_minutes: int = Field(30, ge=1)   # wall time of one remote attempt
```

`validate_config` adds: every `delivery_harnesses[*].harness` must be in `allowed_harnesses` and in the
known names; `allowed_harnesses` may now contain any known name. `services.ensure_team_agents` sets each
delivery agent's `harness`/`provider` from `delivery_harnesses` (cycled) and keeps `model` from routing as
today. Judges and the coordinator keep `daash`.

**Model for a remote attempt**: exactly as native: `choose_model(DELIVERY, cfg, complexity, attempts,
stats)` (the gateway serves the same aliases to every harness). If the agent's HarnessSpec has a non-empty
`model`, that is the base instead, escalated with `escalate_model(spec_model, cfg, attempts)`.

## 3. Data (migration `f0a3c0d0e002_runners`, down_revision = 0.1 head)

```sql
CREATE TABLE runners (
  id text PRIMARY KEY, name text NOT NULL UNIQUE,
  token_hash text NOT NULL UNIQUE,            -- sha256 hex of the bearer token
  enabled boolean NOT NULL DEFAULT true,
  host text, version text,
  harnesses jsonb NOT NULL DEFAULT '[]',      -- [{"harness": "codex", "models": ["fast", ...]}]
  max_jobs int NOT NULL DEFAULT 1,
  last_seen_at timestamptz, created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE remote_jobs (
  id text PRIMARY KEY,
  swarm_id text NOT NULL REFERENCES swarms(id) ON DELETE CASCADE,
  team_id text NOT NULL, task_id text NOT NULL, agent_id text NOT NULL,
  attempt int NOT NULL,                       -- task.attempts when created (0-based)
  harness text NOT NULL, model text NOT NULL,
  status text NOT NULL DEFAULT 'queued',      -- queued|leased|completed|cancelled|abandoned
  runner_id text REFERENCES runners(id) ON DELETE SET NULL,
  payload jsonb NOT NULL,                     -- what the runner gets (section 5.2)
  result jsonb,                               -- what the runner returned (section 5.5)
  expiries int NOT NULL DEFAULT 0,            -- leases that timed out (runner vanished)
  lease_expires_at timestamptz, heartbeat_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), leased_at timestamptz, finished_at timestamptz);
CREATE UNIQUE INDEX remote_jobs_one_active ON remote_jobs(task_id) WHERE status IN ('queued','leased');
```

Repos (app/repo.py): `RunnerRepo` (create, get, by_token_hash, list, update, delete, touch),
`RemoteJobRepo` (ensure_active(task_id, attempt, ...) returning the existing queued/leased job for the task
or creating one, get, list(swarm_id), lease_next(runner, harness_models, now, lease_seconds) using
`FOR UPDATE SKIP LOCKED`, heartbeat, complete(result), cancel, requeue_expired(now) (leased and
`lease_expires_at < now` → `queued`, `runner_id` NULL, `expiries += 1`)). Runner "online" = `enabled` and
`last_seen_at` within 60 s.

## 4. Authentication

* **Runner tokens.** Format `daash_rt_` + 43 url-safe chars (`secrets.token_urlsafe(32)`). Only the sha256
  hex is stored; compare with `hmac.compare_digest`. Every `/runner/*` endpoint requires
  `Authorization: Bearer <runner token>` of an **enabled** runner: 401 otherwise (missing/unknown/disabled).
* **Admin token.** `DAASH_API_TOKEN` (Settings.API_TOKEN, default ""). When non-empty every endpoint except
  `GET /health` and `/runner/*` requires `Authorization: Bearer <API_TOKEN>` (401 `{"detail": "Authentication
  required"}`). When empty the admin API is open (local prototype; the coordinator logs a warning at start).
  `GET /health` → `{"status": "ok", "auth_required": <bool>}`.
* **Bootstrap runner.** If `DAASH_RUNNER_BOOTSTRAP_TOKEN` is set, the coordinator ensures at start a runner
  named `local-runner` with that token (hash updated if it changed). Lets docker compose start a runner
  without a manual token step.
* Admin runner management (admin auth):
  * `POST /runners` `{"name": str}` → 201 `{"runner": Runner, "token": "daash_rt_..."}` (token shown once).
  * `GET /runners` → `{"runners": [Runner]}`; `PATCH /runners/{id}` `{"enabled": bool}` → Runner;
    `DELETE /runners/{id}` → 204 (revokes; its leased jobs are requeued).
  * Runner JSON: `{id, name, enabled, online, host, version, harnesses, max_jobs, active_jobs,
    last_seen_at, created_at}` (never the hash).
  * `GET /remote-jobs?swarm_id=` → `{"jobs": [RemoteJob without payload.system_prompt/prompt bodies
    longer than 2000 chars]}` for the dashboard.

## 5. Runner protocol (`/runner/*`, runner token)

5.1 `POST /runner/hello` `{"host": str, "version": str, "max_jobs": int, "harnesses": [{"harness": str,
"models": [str]}]}` → 200 `{"runner_id", "name", "poll_seconds": 5, "heartbeat_seconds": 15,
"lease_seconds": 60}`. Stores capabilities, touches `last_seen_at`. Runners call it at start and every
5 minutes. Unknown harness names are ignored (not an error).

5.2 `POST /runner/lease` `{"wait_seconds": 0..30, "free_slots": int}` → 200 `{"job": Job}` or 204.
Long-poll: checks for a matching queued job about every 1 s until `wait_seconds`. Matching: job.harness is
in the runner's harnesses and (that harness's `models` is empty or contains job.model); oldest first; only
jobs of a live swarm in ACTIVE state; requeue expired leases first. Leasing sets status `leased`, runner,
`leased_at`, `heartbeat_at`, `lease_expires_at = now + 60 s`. Touches `last_seen_at`. `free_slots == 0` →
204 immediately (but still touches). Job:

```json
{"id": "...", "swarm_id": "...", "task_id": "...", "attempt": 0,
 "harness": "codex", "model": "fast",
 "system_prompt": "<the agent's composed Vision→Values→Mission system prompt>",
 "prompt": "<remote work prompt, section 6>",
 "limits": {"timeout_seconds": 1800, "max_files": 200, "max_file_bytes": 2000000,
            "max_total_bytes": 20000000},
 "task": {"name": "...", "artifact": "guides/basil.md"},
 "workspace_url": "/runner/jobs/<id>/workspace"}
```

5.3 `GET /runner/jobs/{id}/workspace` → 200 `application/gzip`: a tar.gz of the swarm workspace
(`WORKSPACE_ROOT/<swarm_id>/`), regular files only, excluding the `.daash/` folder. 404 unless this runner
holds the lease (status `leased`).

5.4 `POST /runner/jobs/{id}/heartbeat` `{"note": str?}` → 200 `{"status": "leased" | "cancelled"}`;
extends the lease by 60 s. `cancelled` (swarm paused/verified, task changed, job abandoned) tells the
runner to stop the CLI and drop the job. 404 if the job is not this runner's.

5.5 `POST /runner/jobs/{id}/result` (JSON, body ≤ 25 MB) →
200 `{"status": "completed"}` | 409 if the job is not leased by this runner (e.g. cancelled/expired).

```json
{"outcome": "submitted" | "failed" | "error",
 "summary": "the agent's final message (≤ 4000 chars)",
 "files": [{"path": "guides/basil.md", "content_b64": "..."}],   // created or modified
 "deleted": ["old.md"],
 "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
 "cost_usd": null, "duration_ms": 0, "exit_code": 0,
 "transcript_tail": "≤ 20000 chars of the CLI's output",
 "error": "for failed/error: what went wrong"}
```

* `submitted`: the CLI finished normally (exit 0 with a final answer). `failed`: the agent did not finish
  (non-zero exit not caused by infrastructure, timeout, no final answer) — counts as an attempt.
  `error`: infrastructure (CLI missing, model gateway unreachable/5xx, auth failure, runner crash) — does
  **not** count an attempt; the engine retries with its normal cooldown.
* The coordinator validates every path (relative, no `..`, no absolute, no NUL, not under `.daash/`, not a
  CLI state folder: `.git/ .claude/ .codex/ .openclaude/ node_modules/`), the limits, and base64. Invalid →
  400 with the reason (runner should report `error`). Valid files are written to
  `WORKSPACE_ROOT/<swarm_id>/.daash/staging/<job_id>/` (plus `deleted` in the result JSON); the job becomes
  `completed` with `result` (file contents removed from the stored JSON; paths kept).

## 6. Engine and jobs

`Jobs.work_task` branches on the assigned agent's harness: `daash` → native loop (unchanged); anything
else → `Jobs._remote_work(...)`:

1. Same preamble as native: `busy(agent, task)`, classification, model choice, the agent system prompt.
2. `prompt = prompts.remote_work_prompt(task, criteria, feedback, workspace_listing)`: the task, its
   artifact and acceptance criteria, feedback from the previous attempt, the file list, and these rules:
   work only inside the current directory (it is a copy of the shared swarm workspace); create/edit files
   with your own tools; you may run code and look things up if your harness allows; do not ask questions;
   when finished, reply with a short summary of what you did and how each criterion is met.
3. `job = RemoteJobRepo.ensure_active(task.id, task.attempts, ...)` — an engine restart re-attaches to the
   same job instead of creating a duplicate.
4. Poll the job every 2 s (`asyncio.sleep`; tests may shrink it via `Jobs.remote_poll_seconds`):
   * `requeue_expired(now)`; if `expiries >= 3` → abandon the job and raise an infrastructure error
     (`LLMError(code="remote")`-like `RemoteError` treated by the engine as infra: cooldown, no attempt).
   * `queued` and no lease for `remote_fallback_minutes` → mark `abandoned`, event
     *"No runner offering '<harness>' picked up '<task>' within N minutes, so Delivery-k ran it on the
     built-in harness instead."*, then run the native loop for this attempt.
   * `leased` for longer than `remote_job_timeout_minutes` → cancel; attempt failed (*"the remote agent
     ran out of time"*).
   * `completed` → apply (below).
5. Apply: `_require_state(swarm)`; task still In-Progress (else drop, as native). Move staged files into
   the workspace (replace), delete `deleted` paths (inside the workspace only), remove the staging dir.
   `record_call(agent, "work_task", model, usage, transcript=[tail], ok=outcome != "error")`, with
   `harness`/runner in the transcript meta. Then by outcome:
   * `submitted` → Review with `deliverable = {"summary", "files", "model", "harness", "runner"}`, event
     *"Delivery-2 (codex on runner 'mac-mini') submitted ..."*, signal `task.submitted`.
   * `failed` → `_attempt_failed(...)` with the runner's error as the reason (files kept as native).
   * `error` → raise `RemoteError` (infra; job stays `completed` for audit; the next WORK_TASK creates a new
     job for the same attempt).
6. `asyncio.CancelledError` (pause/verify, B.4) → mark the job `cancelled` (shielded, best effort) so the
   runner learns it from its next heartbeat; re-raise.

`ActionKind`/`decide()` do not change: a remote WORK_TASK is a long-running job like a native one. The
dashboard shows the agent BUSY on the task while the runner works.

## 7. The runner (`daash_runner/`, Python standard library only)

```
python3 -m daash_runner --coordinator http://host:8000 --token daash_rt_... \
    --config runner.json --work-dir ~/.daash-runner --max-jobs 2
```
(all flags also from env: `DAASH_COORDINATOR_URL`, `DAASH_RUNNER_TOKEN`, `DAASH_RUNNER_CONFIG`,
`DAASH_RUNNER_WORKDIR`, `DAASH_RUNNER_MAX_JOBS`.) Python ≥ 3.10, no third-party packages, so it runs on
any device without a venv.

* `runner.json`: `{"name": "...", "harnesses": {"<name>": {"command": "codex", "models": [...],
  "env": {"K": "V or ${ENV_VAR}"}, "args": [...extra], "timeout_seconds": 1800}}}`. Only configured
  harnesses whose command is found on PATH are announced in `hello`.
* Loop: hello → while running: lease (long-poll) when a slot is free → run job in a worker thread →
  heartbeat every 15 s while it runs → upload result. Re-hello every 5 minutes; exponential backoff on
  coordinator errors; clean shutdown on SIGINT/SIGTERM (stop CLIs, report `error` for running jobs).
* Job: `<work-dir>/jobs/<job_id>/ws/` ← safe-extract the workspace tar (reject absolute/`..`/links);
  snapshot (path → sha256); run the adapter's argv with `cwd=ws`, new process group, the job timeout;
  on `cancelled` heartbeat or timeout → kill the group. Changed/new files = hash differs; deleted =
  missing; ignore CLI state folders (section 5.5 list). Upload; keep the job folder (logs) and prune
  folders older than 3 days. Runner log: `<work-dir>/runner.log` (rotating).
* Adapters (`daash_runner/adapters.py`): each builds argv/env from (system_prompt, prompt, model) and
  parses stdout → `(final_text, usage, cost_usd, outcome, error)`.
  * `openclaude`: print mode with stream-json output, the model alias, full-access permission mode inside
    the job folder, system prompt appended; provider = the OpenAI-compatible gateway (env).
  * `claude-code`: `claude -p --output-format stream-json --verbose --model <m> --append-system-prompt ...`
    with `ANTHROPIC_BASE_URL` = the gateway (it serves the Anthropic Messages API) and `ANTHROPIC_API_KEY`;
    `CLAUDE_CONFIG_DIR` = a runner-private folder so the user's own Claude Code settings, history and
    subscription are never used.
  * `codex`: `codex exec --json --skip-git-repo-check --sandbox workspace-write -m <m>` with a custom
    model provider pointing at the gateway (Responses API) via `-c` overrides; `CODEX_HOME` = a
    runner-private folder.
  * `fake`: `python3 -m daash_runner.fake_cli` — deterministic: writes the task's artifact with content
    that satisfies typical deterministic criteria, prints a stream-json-like final message; env
    `DAASH_FAKE_MODE` = `ok` | `fail` | `error` | `hang` | `slow` for tests.
  The exact flags are verified against the installed CLI versions (OpenClaude 0.31, Claude Code 2.1,
  Codex 0.160) and recorded in docs/RUNNER.md.
* Docker: `Dockerfile.runner` (python3 + node + `npm i -g @gitlawb/openclaude @anthropic-ai/claude-code
  @openai/codex`), compose service `runner` (profile `runner`, not started by default) using
  `DAASH_RUNNER_BOOTSTRAP_TOKEN`, the gateway URL/key from `.env`, and `runner/runner.docker.json`. Running
  CLIs inside the container keeps agent-executed code off the host. A host runner is supported for devices.

## 8. DAASHboard

* Login: when `/api/health` says `auth_required` (or any call returns 401) show a token prompt; keep the
  token in localStorage; send `Authorization: Bearer` on every request; "Sign out" clears it.
* Runners panel (always visible): name, online dot, host, harnesses, active/max jobs, last seen,
  enable/disable, delete; "Add runner" → name → shows the token once with a copy button and the
  `python3 -m daash_runner ...` command line.
* Create-swarm form: delivery-harness mix rows (harness select from allowed harnesses, optional model) and
  `allowed_harnesses` checkboxes; roster shows each agent's harness next to its model; task drawer and
  activity show the harness/runner for remote submissions; remote jobs list (status, runner, age) per swarm.

## 9. Tests

* unit: path validation, token hashing, lease matching (pure function over job + capabilities),
  delivery-harness cycling, config validation, remote prompt; runner: tar safe-extract, change detection,
  each adapter's argv/env and output parsing from recorded fixtures, config loading/env expansion.
* integration (real DB, ASGI): every runner/admin endpoint and status code, auth on/off, bootstrap runner;
  the engine with an **in-process fake runner** (a coroutine that leases over ASGI and posts results): mixed
  team to VERIFICATION; restart re-attaches to the same job; pause cancels the remote job and the heartbeat
  says `cancelled`; lease expiry → requeue → another runner completes; `error` → cooldown, no attempt;
  `failed` → rework; fallback to native when no runner; result path validation.
* e2e (`scripts/e2e.sh`, tests/e2e/test_runner_scenarios.py): `runner` and `runner-hang` services built from
  the `fake-only` stage of `Dockerfile.runner` (python only, `runner/runner.e2e.json`: only the `fake` harness),
  both using the bootstrap token `local-runner` (`DAASH_RUNNER_BOOTSTRAP_TOKEN`); `DAASH_RUNNER_LEASE_SECONDS=5`
  (default 60; heartbeat = lease/3, returned in hello). `runner` (fake CLI takes ~4 s) runs always; `runner-hang`
  (CLI hangs) is created but stopped until the test starts it. Scenarios: mixed native+fake swarm to
  VERIFICATION with `/remote-jobs` runner names; runner killed mid-job, lease expires, the other container
  finishes it with no duplicate completed jobs; bad/missing token -> 401 and open admin API; pause cancels the
  remote job, the runner drops it, no fake-LLM calls while paused, resume completes.
* live (manual, gateway): a swarm whose delivery agents are native + openclaude + claude-code + codex in the
  runner container.

## 10. Vision review (vision-level Definition of Done)

Found in the 0.3 live run (and in the First Garden retrospective): when every team finished, the swarm went
straight to VERIFICATION. Nothing checked the **vision-level** Definition of Done or consistency across
teams; a README team "accomplished" its mission before the module it documents existed. README: "DoD has a
vision level and a team-mission level".

* `ActionKind.REVIEW_VISION` (append to the enum). `decide()`: when the swarm is ACTIVE and every team is
  terminal (accomplished/failed), emit `REVIEW_VISION` instead of `COMPLETE_SWARM`, unless the swarm already
  used `MAX_VISION_REVIEWS = 3` reviews (then `COMPLETE_SWARM` as before).
* Migration `f0a3c0d0e003_vision_review`: `swarms.vision_reviews int NOT NULL DEFAULT 0`.
* Job `review_vision` (the Swarm Coordinator agent, its configured model; JSON job with the usual parse
  re-ask): prompt = the Vision, the vision-level DoD, each team (name, mission, status, Done task names),
  the workspace file list, and the deliverable files' contents (bounded as the review job does). Contract:
  `{"verdict": "met" | "gaps", "confidence": 0..1, "summary": "...",
    "gaps": [{"team": "<exact team name>", "focus": "what that team must change or add"}]}`.
  Parser: `gaps` must be non-empty unless verdict is `met` with confidence ≥
  `dod_thresholds.mission_confidence`; unknown team names are rejected (re-ask lists the valid names).
* Outcome: increment `vision_reviews`. Met (≥ threshold) → event "Swarm Coordinator confirmed the vision-level
  Definition of Done (confidence c): summary" then the COMPLETE_SWARM path (verification). Gaps → for each
  named team: status `active`, `next_focus` = focus, event "Swarm Coordinator reopened team 'X': focus";
  the team then plans (its tasks are Done and already assessed, so `decide()` emits PLAN_TASKS with the
  focus). A failed team can be reopened only if it still has task budget; otherwise its gap is reported
  in the event and ignored. If no named team can act, complete the swarm (event lists the open gaps).
* The calibrated certainty scale applies (DESIGN-0.1 §12): a confidence below 1.0 must name a concrete gap.
* Fake LLM (tests/fakes): answers `review_vision` with `met` by default; scenario `vision_gaps_first: N`
  makes the first N reviews return a gap for the first team.

## 11. Agent capabilities (user request, 2026-11-09)

Found live: rotation gave "verify the test suites pass with `python3 -m unittest`" to a native agent that
cannot run code; it "verified" by reading, and the judges rightly sent it back (no evidence). The user asked
that each team member have a basic capabilities list that depends on its harness.

* Vocabulary (`app/models.py` `Capability`): `edit_files` (create/modify workspace files — every harness),
  `run_code` (execute shell commands, scripts and tests), `web_research` (search/fetch the web).
* **Defaults per harness** (`HARNESS_CAPABILITIES`): `daash` → [edit_files]; `openclaude`, `claude-code`,
  `codex` → [edit_files, run_code]; `fake` → [edit_files, run_code]. (Web research is off by default: the
  runner configs disable hosted web tools the local gateway cannot serve.) `HarnessSpec.capabilities:
  Optional[list[str]]` overrides the default for that delivery slot. Future harnesses (e.g. OpenClaw) add an
  adapter plus a default entry.
* **Runners announce** per harness: hello `harnesses: [{"harness", "models", "capabilities"}]`, from
  `runner.json` (`"capabilities": [...]`) or the adapter default: container config → edit_files+run_code;
  host example with `acceptEdits` permission mode → edit_files only. A job is leased only by a runner whose
  harness entry offers the job's `required_capabilities` (missing `capabilities` in an old hello = harness
  default).
* **Agents**: migration `f0a3c0d0e004_capabilities`: `agents.capabilities jsonb NOT NULL DEFAULT '[]'`
  (set by `ensure_team_agents` from the spec/default; existing rows backfilled from harness defaults), and
  `tasks.required_capabilities jsonb NOT NULL DEFAULT '[]'`; `remote_jobs` payload carries them.
* **Tasks**: the plan contract gains optional `"needs": ["run_code" | "web_research"]` per task (the prompt
  explains: ask for `run_code` when a criterion needs a command, script or test run as evidence). The
  planner sees which capabilities the team's delivery agents have (do not plan needs nobody has; plan
  read-only verification instead). `edit_files` is implied. Unknown values are dropped. `create_subtask` and
  `POST /tasks` accept `required_capabilities` too.
* **Assignment** (`decide()`): an idle delivery agent commits only to a Ready task whose
  `required_capabilities ⊆ agent.capabilities` (still least-recently-active first). The DoR "idle delivery
  agent" rule becomes "an idle delivery agent with the required capabilities". If **no** delivery agent of the
  team has them, the task is not stuck forever: it is assigned to any idle agent and the work prompt says
  which capabilities it lacks (degraded), with a one-time event.
* **Rework**: when a task returns to In-Progress and its assigned agent lacks a required capability while a
  capable idle agent exists, it is reassigned (event).
* **Prompts**: every delivery agent's work prompt states "Your capabilities: …; you cannot …" so it never
  claims to have run code it could not run; review prompts tell the judges which capabilities the delivering
  agent had.
* **Dashboard**: capability chips on the roster (e.g. `run code`), task cards/drawer show `needs`, the create
  form's delivery rows allow overriding capabilities, the runners panel shows per-harness capabilities.
* **Tests**: unit (defaults, subset assignment, degraded fallback, plan parsing of needs, prompt text, lease
  matching with capabilities); integration (a `run_code` task goes to the remote agent, never the native one;
  degraded fallback when no agent can; rework reassignment); fake LLM: scenario `plan_needs` to mark planned
  tasks with needs; e2e unchanged but must pass.
