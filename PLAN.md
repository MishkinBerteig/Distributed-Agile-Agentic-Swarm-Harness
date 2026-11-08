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
  (OpenClaude, Claude Code, Codex…) via the host runner (`app/runner.py`) are deferred to after 0.1.
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

### Milestone 0.1 — Prototype (in progress)
Corrects earlier misreadings of the README (see DESIGN-0.1 §1): agentic (not embedding-based) Mission Judgement,
deterministic DoR gate and thresholded two-level DoD, Swarm Coordinator + swarm configuration + global Values &
Principles, multi-criterion green/red tasks, cycle-time/error/failure metrics, team spin-out by majority vote,
learning mechanisms, budgets.
- [ ] Data layer: new schema migration, repositories, snapshot loader
- [ ] Domain logic: lifecycle/task transitions, DoR gate, DoD evaluation, voting, limits, routing, metrics, checks
- [ ] Native harness: LLM client + tool loop, sandboxed workspace tools, prompts + parsers
- [ ] Engine: pure `decide()` + executor jobs (bootstrap, standards, plan, prioritize, commit, work, review, assess,
      materialize team, retrospective)
- [ ] API per DESIGN-0.1 §9; compose `engine` service + workspace volume
- [ ] DAASHboard rewrite per DESIGN-0.1 §10
- [ ] Live smoke run against the m3u512gb gateway (a small vision driven to VERIFICATION)

### After 0.1
- Revive integration tests; add E2E (create → run → verify), restart-resilience, security and performance tests.
- External CLI harnesses through the host runner (heterogeneous harnesses across devices — the "distributed" part).
- Learning mechanism 2: execute agent-written automation scripts in a sandbox container.
- Real embedding model by default; richer RAG over transcripts.
- Multiple providers in one swarm; authentication; push updates (SSE) instead of polling.

## 6. Risks
- **Local model reliability** (malformed JSON, missing tool calls): tolerant extraction, one re-ask, bounded tool
  steps, attempts → Failed, model escalation up the ladder.
- **Runaway cost/time**: token, wall-clock and USD limits pause the swarm automatically.
- **State consistency**: PostgreSQL is the only source of record; Redis holds only expiring signals; the engine is
  restart-safe (BUSY agents reset, open tasks resumed).

## 7. Current System State
Being rebuilt for 0.1 on branch `prototype-0.1`; see the milestone checklist above.

## 8. Completed Increments
- **2026-09-28 — Slice 1 / 1.5**: durable task state, embeddings, first DAASHboard.
- **2026-09-30 — Slice 2**: Decision Bus with atomic quorum voting.
- **2026-10-01 — Slice 3**: harness adapters; swarm lifecycle state machine; single-live-swarm invariant.
- **2026-10-03 — Slice 4 / 5.0**: Vision/Mission prompts; LLM-generated team DoR/DoD.
- **2026-10-09 — Slice 5.1 (partial)**: host runner + session orchestration (superseded for 0.1).
- **2026-11-08 — 0.1 design**: DESIGN-0.1 contract, domain model, unit/integration test split, runner spawn fix.
