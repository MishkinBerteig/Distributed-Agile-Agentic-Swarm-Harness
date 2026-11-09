# Implementation Plan: Distributed Agile Agentic Swarm Harness (DAASH)

## 1. System Overview
DAASH is a containerized harness for running a swarm of AI-agent teams toward a long-running Vision.
Alignment comes from a deterministic prompt hierarchy (Vision → Values & Principles → Mission → Definitions of
Done/Ready → Role). Commitment and completion are gated by a deterministic Definition of Ready and a two-level
Definition of Done judged by Mission Judgement and Quality Judgement agents. Progress is visible in a single
human-readable task database with p90 cycle time, error rates and team failure rates.

### Core Constraints
- **Containerization**: Docker (and Podman-compatible compose).
- **Persistence**: Full system state survives container shutdown/restart (PostgreSQL is the source of record).
- **Scale**: High token volume (10M+ context, 100M+ thinking tokens) — budgets and limits are first-class.
- **Visibility**: Human-readable task database, event log and metrics (DAASHboard).

## 2. Technology Stack
- **Python 3.11**: FastAPI coordinator API + async engine; Pydantic models; asyncpg.
- **PostgreSQL 17 + pgvector**: all durable state; hand-written Alembic migrations.
- **Redis**: Decision Bus (ephemeral signals, quorum voting).
- **LLMs**: any OpenAI-compatible endpoint. Default is the m3u512gb private gateway (aliases `instant`, `fast`,
  `great`, `amazing`; reasoning models with tool calling), reached at `m3u512gb.tail391a32.ts.net:12345`
  because `m3u512gb.local` (mDNS) does not resolve inside containers. Key lives in the gitignored `.env`.
- **Harness**: native `daash` harness (OpenAI-compatible tool-calling loop in `app/llm.py`). External CLI harnesses
  (OpenClaude, Claude Code, Codex…) via the pull-based runner (`daash_runner/`) are deferred to after 0.1.
- **DAASHboard**: Vite + React.

## 3. Architecture
The binding design for the 0.1 prototype is **docs/DESIGN-0.1.md** (modules, schema, domain rules, engine
algorithm, API, dashboard, testing policy). Summary:
- `coordinator` (FastAPI :8000) — REST API, runs migrations.
- `engine` (`python -m app.engine`) — ticks every 3s: loads a snapshot of the live swarm, a pure `decide()` picks
  actions, an executor runs them (LLM jobs bounded by `max_simultaneous_agents`).
- Agents per team: Mission Judgement, Quality Judgement, N × Mission Delivery; one Swarm Coordinator per swarm.
- Artifacts are files in `/workspaces/<swarm_id>/` (named volume), checked by deterministic acceptance criteria.

## 4. Execution Model (XP)
Small slices, continuous integration, everything built and run in Docker
(`docker compose up --build -d`; unit tests `docker compose --profile test run --rm --build test`).

**Testing policy until the 0.1 prototype is accepted:** only fast, isolated unit tests (pure logic, parsers,
sandboxed tools, mocked HTTP) in `tests/unit/`. The earlier DB/Redis tests live in `tests/integration/`
(not run by default). E2E, resilience, security and performance testing resume after 0.1.

## 5. Backlog

### Completed (pre-0.1 slices)
Slices 1–5.0: durable task state, first DAASHboard, Decision Bus with atomic quorum voting, harness adapter
interface, Vision/Mission prompt composition, LLM-generated team DoR/DoD. Slice 5.1 (host runner + OpenClaude
sessions) was built but never ran end to end; it is superseded by the native harness for 0.1.

### Milestone 0.1 — Prototype (ready for user testing)
Corrects earlier misreadings of the README (see DESIGN-0.1 §1): agentic (not embedding-based) Mission Judgement,
deterministic DoR gate and thresholded two-level DoD, Swarm Coordinator + swarm configuration + global Values &
Principles, multi-criterion green/red tasks, cycle-time/error/failure metrics, team spin-out by majority vote,
learning mechanisms, budgets.
- [x] Data layer: new schema migration, repositories, snapshot loader (verified against real PostgreSQL)
- [x] Domain logic: lifecycle/task transitions, DoR gate, DoD evaluation, voting, limits, routing, metrics, checks
- [x] Native harness: LLM client + tool loop, sandboxed workspace tools, prompts + parsers (verified on the gateway)
- [x] Engine: pure `decide()` + executor jobs (bootstrap, standards, plan, prioritize, commit, work, review, assess,
      vote/expire proposals, materialize team, fail team, complete swarm, retrospective), cooldowns, model escalation
- [x] API per DESIGN-0.1 §9; compose `engine` service + workspace volume
- [x] DAASHboard rewrite per DESIGN-0.1 §10
- [x] Hardening pass (dead-dependency stalls, user overrides vs running jobs, reopen semantics, duplicate proposals)
- [x] Live runs against the m3u512gb gateway: a seeded one-team swarm driven to VERIFICATION + retrospective; a
      coordinator-bootstrapped two-team swarm ("Backyard Birds") run through the API/dashboard, including a mid-run
      engine rebuild/restart and a user-added task

### Milestone 0.2 — Test harness + throughput (done; tagged `v0.2`)
Design: docs/DESIGN-0.2.md. (`v0.1` is tagged at f1e5526.)
- [x] Scripted fake LLM (in-process and as a container) with scenarios and call recording
- [x] Integration tier: real PostgreSQL/Redis, API in-process, engine in-process driven to VERIFICATION
- [x] E2E tier: real containers + fake-LLM container, incl. engine restart mid-run, pause stops spending
- [x] Batch approval (Ready buffer = delivery agents), judges run concurrently, per-model LLM concurrency
- [x] Leaving ACTIVE cancels running jobs; verification closes open tasks
- Verified: unit 384 passed (~1 s); integration 99 passed (~45 s); e2e 8 passed (~2 min). Live on the gateway, a
  2-delivery-agent swarm had two tasks approved in one call and both agents committing in the same second; 4/4
  tasks Done on the first attempt, p90 cycle time 4.7 min (v0.1 runs: 11–13 min).
- [x] B.6 deferral (after the tag): Mission Judgement can approve nothing when a task needs another team's work;
  the task stays Pending and the engine re-asks when more work is Done (no rejection churn, no hot loop).
  Deferral is offered only while other work is in flight, so it cannot wait forever. unit 398, integration 103,
  e2e 8. Live: the First Garden re-run finished 9/9 tasks Done on the first attempt, 3 teams accomplished,
  0 failed, 55 min, 516k tokens.

- [x] Post-run fixes: tasks rejected before anyone started them no longer use the team's task budget (bounded by
  the same limit as a runaway guard); Quality Judgement's Definition of Ready may not forbid a file from
  existing (it blocked the team's own refinement tasks).

### After 0.2 (in priority order)
1. **Distributed, heterogeneous harnesses**: external CLI agents (OpenClaude, Claude Code, Codex) on any device
   through the pull-based runner (`daash_runner/`), plus authentication. These harnesses bring their own code execution
   and web research, so this item largely covers items 2 and 3.
2. Sandboxed code execution for the native harness (learning mechanism 2: run agent-written automation scripts;
   test-based deterministic checks).
3. Research tools for the native harness (web fetch/search with required citations) so fact-checking is grounded.
4. Long soak run at README scale (10M+ tokens); real embedding model by default; richer RAG over transcripts;
   multiple providers per swarm; push updates (SSE) instead of polling.

## 6. Risks
- **Local model reliability** (malformed JSON, missing tool calls): tolerant extraction, one re-ask, bounded tool
  steps, attempts → Failed, model escalation up the ladder.
- **Runaway cost/time**: token, wall-clock and USD limits pause the swarm automatically.
- **State consistency**: PostgreSQL is the only source of record; Redis holds only expiring signals; the engine is
  restart-safe (BUSY agents reset, open tasks resumed).

## 7. Current System State (0.1 prototype, on `main`)

### Run it
```bash
cp .env.example .env            # set DAASH_LLM_API_KEY (and DAASH_LLM_ENDPOINT if not the m3u512gb gateway)
docker compose up --build -d    # db, redis, coordinator :8000, engine, dashboard :7173
docker compose --profile test run --rm --build test   # unit tests
```
DAASHboard: http://127.0.0.1:7173 — create a swarm (vision; optional teams, else the Swarm Coordinator forms them;
advanced config for role models and limits), press ▶ Start, watch teams, Kanban, task drawers (criteria, verdicts,
timeline, artifacts), metrics, activity feed. API docs: http://127.0.0.1:8000/docs.

### Services
- **coordinator** — FastAPI (`app/main.py`), runs `alembic upgrade head` at start; healthcheck on `/health`.
- **engine** — `python -m app.engine`; restart-safe; `DAASH_ENGINE_ENABLED=false` idles it.
- **db** (PostgreSQL 17 + pgvector, volume `pg-data`), **redis** (Decision Bus), **dashboard** (Vite dev server).
- Volume `daash-workspaces` holds each swarm's artifacts at `/workspaces/<swarm_id>/`.

### Code map (`app/`)
`models.py` contract · `policy.py` / `routing.py` / `metrics.py` pure rules · `checks.py` deterministic criteria ·
`llm.py` + `tools.py` native harness · `prompts.py` prompt hierarchy + parsers · `repo.py` data access ·
`services.py` shared team/agent/prompt/accounting helpers · `engine.py` (`decide()` + loop) and `jobs.py` ·
`main.py` + `api_helpers.py` API · `bus.py` Decision Bus · `embeddings.py` · `daash_runner/` (stdlib pull runner for external CLI harnesses; 0.3).

### Defaults worth knowing
- Role models: coordinator `amazing`, judges `great`, delivery `fast` (escalates up `instant→fast→great→amazing`
  on failed attempts and on learned low success rates), classifier `instant`.
- Limits: 5M tokens per swarm, 3 simultaneous agent jobs, 4 teams, 25 tasks per team, 3 attempts per task.
- DoD thresholds are the README's (deterministic 100%, rubric > 99.9%, mission confidence ≥ 99%); judge prompts are
  calibrated so these are reachable (DESIGN-0.1 §12).
- Only one live swarm at a time; archive or delete it to create the next.

### Tests (three tiers, docs/DESIGN-0.2.md)
- **unit** — `docker compose --profile test run --rm --build test` — pure logic, ~1 s.
- **integration** — `docker compose --profile integration run --rm --build integration` — real PostgreSQL/Redis
  (`daash_test`, Redis DB 15), API and engine in-process against the scripted fake LLM (`tests/fakes/`).
- **e2e** — `scripts/e2e.sh` — separate compose project `daash-e2e` (no published ports, safe beside the dev
  stack): real coordinator/engine containers + fake-LLM container; includes engine restart mid-run.
- New fake-LLM scenario: add a field to `Scenario` in `tests/fakes/fake_llm.py`, read it in the job's `_answer_*`
  method, then `fake.configure({...})` in a test.

### Known limitations (0.1)
- Agents can only read/write files in their swarm workspace; no code execution (automation scripts are recorded,
  not run). External CLI harnesses are not wired.
- Judges are LLMs: verdicts are good but not infallible (e.g. minor factual slips can pass a rubric).
- Dashboard polls every 3s; no auth; CORS open — local prototype only.
- The pre-0.1 dev database was replaced by the new schema (a dump of the old data is in
  `agents-workspace/coordinator/db-backup/`).

## 8. Completed Increments
- **2026-09-28 — Slice 1 / 1.5**: durable task state, embeddings, first DAASHboard.
- **2026-09-30 — Slice 2**: Decision Bus with atomic quorum voting.
- **2026-10-01 — Slice 3**: harness adapters; swarm lifecycle state machine; single-live-swarm invariant.
- **2026-10-03 — Slice 4 / 5.0**: Vision/Mission prompts; LLM-generated team DoR/DoD.
- **2026-10-09 — Slice 5.1 (partial)**: host runner + session orchestration (superseded for 0.1).
- **2026-11-08 — 0.1 design**: DESIGN-0.1 contract, domain model, unit/integration test split, runner spawn fix.
- **2026-11-08 — 0.1 prototype**: data layer, domain logic, native harness, engine, API, DAASHboard, hardening and
  team-voting passes built by parallel sub-agents against DESIGN-0.1; judge calibration; verified live on the
  m3u512gb gateway.
- **2026-11-08 — 0.2**: three-tier test harness with a scripted fake LLM; batch approval, concurrent judges,
  per-model LLM concurrency, cancel-on-pause and verification task closing. Tagged `v0.2`.
- **2026-11-08 — 0.2 B.6**: deferral of not-yet-startable tasks (cross-team dependencies), found while re-running
  First Garden.
