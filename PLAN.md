# Implementation Plan: Distributed Agile Agentic Swarm Harness (DAASH)

## 1. System Overview
DAASH is a containerized harness for managing distributed swarms of AI Agents organized into teams. It keeps agents aligned through a hierarchical prompt structure (Vision $\rightarrow$ Mission) and enforces quality and mission gates.

### Core Constraints
- **Containerization**: Docker and Podman.
- **Persistence**: Full system state survives container shutdown/restart.
- **Scale**: High token volume (10M+ context, 100M+ thinking tokens).
- **Visibility**: Human-readable global task database with performance metrics (p90 cycle time, error rates).

## 2. Technology Stack
- **Language**: Python (FastAPI coordination API, Pydantic validation).
- **Database**: PostgreSQL + pgvector, SQLAlchemy 2.0 (async), Alembic migrations.
- **Message Broker**: Redis (inter-agent decisions and high-frequency signals).
- **Frontend**: DAASHboard — Vite + React.
- **Persistence**: Named Docker/Podman volumes.
- **Containerization**: Multi-stage Dockerfile and `docker-compose.yaml`.
- **Supported Harnesses**: Adapters for LMStudio (implemented), OpenClaw, Hermes Agent, OpenClaude, Claude Code, Codex, and OpenWebUI.

## 3. Architectural Components
- **Swarm Coordinator**: Top-level orchestrator managing the Vision Statement, team lifecycle, and resource budgets.
- **Team Structure**: Mission Judgement (alignment), Quality Judgement (DoD audit), and Mission Delivery Agents.
- **Task Coordination System (Ground Truth)**: Global database holding task state (Pending $\rightarrow$ Ready per DoR $\rightarrow$ In-Progress $\rightarrow$ Done), all non-decision communication and deliverables, and p90 cycle time / failure rate metrics.
- **Decision Bus (Redis)**: Ephemeral signals for coordination and voting.
- **Persistent Memory**: RAG (vector store), audit trail (transcripts), and task history.

## 4. Extreme Programming (XP) Execution Model
DAASH is built with XP: incremental delivery of small, testable slices under continuous integration.

### Definition of Done
1. **Unit Testing (TDD)**: Every logic change starts with a failing test; high coverage of business logic, validators, and adapters.
2. **E2E Testing**: Full container orchestration tests, including state resilience (`stop` $\rightarrow$ `start` $\rightarrow$ zero data loss).
3. **Security Testing**: Input validation on all agent-submitted data; secure volume mounting and container isolation.
4. **Performance Testing**: Task DB stress under concurrent agent updates; Redis vs. PostgreSQL latency.

### Design Principles
- **Continuous Refactoring**: The architecture evolves with each story.
- **Simple Design**: The simplest thing that works for the current test case.
- **Small Releases**: Every completed story is deployed and verified in the harness.

## 5. Story Backlog (Prioritized)
- [x] **Slice 1: Durable Task State** — SQLAlchemy models (Tasks/Teams) with pgvector, Alembic migrations, volume persistence, hierarchical task CRUD.
- [x] **Slice 1.5: DAASHboard** — React UI to inspect tasks and teams and create a swarm; task embeddings populated at creation.
- [x] **Slice 2: Decision Bus** — Redis signal feed, atomic quorum voting, E2E consensus test.
- [x] **Slice 3: Harness Adapters** — Adapter interface, LMStudio adapter, `CompositeAdapter`, `build_adapter` factory; smoke-tested against live LMStudio. DAASHboard gains swarm lifecycle controls, Kanban task board, and archived swarm list.
- [x] **Slice 4: Alignment Hierarchy** — Vision/Mission prompt composition (`composeVisionPrompt`, `composeMissionPrompt`) and Mission Judgement endpoint with embedding-based alignment scoring.
- [ ] **Slice 5: Real Agent Swarms & Quality Gates** — launch live agent sessions, DoD/DoR validation, E2E "Task Rejection" flow, updated DAASHboard.
  - [x] **5.0 Quality Judgement generates team DoR/DoD**: The Quality Judgement Agent for each team uses the Swarm Vision, the Team Mission, and any user guidance to create the team's Definition of Ready and Definition of Done (via the harness adapter).
  - [~] **5.1 Launch real agent swarms**: `start` launches live AI agents, end to end through the DAASHboard. Architecture: a **host runner** (`app/runner.py`) — a thin daemon run on each device where harness CLIs are installed (outside compose, published port; future runners on other devices deliver on "distributed"). The coordinator spawns/supervises one headless harness session per team member through the runner; sessions persist in PostgreSQL so `active` survives restarts and stale sessions are reconciled. — Orchestrator, runner daemon, session persistence, roster/reconcile wired to lifecycle + tests added (`tests/test_sessions.py`); live OpenClaude end-to-end run pending.
    - [ ] **5.1.0 Single team, live OpenClaude members**: Given OpenClaude is installed on the local device, pressing ▶ Start on a swarm launches its single team's three members (Mission Judgement, Quality Judgement, Delivery) as separate headless `openclaude` sessions, each carrying the composed Vision→Mission prompt. The DAASHboard shows the swarm ACTIVE with the team roster: every member's role, harness type (OpenClaude), and provider/model; session state (launching/live/failed/stopped) is visible live. Stop terminates all sessions.
    - [ ] **5.1.1 A real agent works a real task**: The Delivery session claims a Ready Kanban task through the coordinator API, does actual work with tool use, and reports back; the DAASHboard shows the task advance In-Progress → Done with the agent's deliverable attached — status advanced by the agent, not by a human moving a card.
    - [ ] **5.1.2 Judge agents gate completion**: Mission Judgement and Quality Judgement review delivered work as live agent turns (replacing embedding-similarity scoring and button-press judgement); a task reaches Done only after both verdicts pass the DoD/alignment gates, votes publish to the Decision Bus, and the UI shows per-task verdicts with reasoning.
    - [ ] **5.1.3 Multiple teams run concurrently**: Starting a swarm with two or more teams launches sessions for every team simultaneously, each with its own Mission; the DAASHboard renders all teams side by side with full member rosters and per-session status.
    - [ ] **5.1.4 Heterogeneous harnesses**: Team members may declare different harnesses (e.g., judges on local LMStudio models, Delivery on OpenClaude) resolved through the CompositeAdapter; the UI shows the actual harness/provider/model per member, and session crashes restart or surface as failed with visible state.
  - [ ] **5.2 Swarm DoR guidance CRUD + team DoR read-only**: As a user, I can CRUD Definition of Ready (Commitment Policy) guidance for the swarm and view the actual Definition of Ready for each Agent Team; regenerating preserves guidance history, standards stay team-wide.
  - [ ] **5.3 Swarm DoD guidance CRUD + team DoD read-only**: As a user, I can CRUD Definition of Done (Fit and Finish) guidance for the swarm and view the actual Definition of Done for each Agent Team.
  - [ ] **5.4 DAASHboard quality-gate visualization**: The DAASHboard shows Agent Teams, Tasks and the team working on each task, each team's DoR and DoD, and Decision Bus activity.
- [ ] **Slice 6: Audit & RAG** — Session transcript storage + vector retrieval tests.
- [ ] **Slice 7: Observability** — Metrics engine + updated DAASHboard.

## 6. Risk Assessment
- **Harness Compatibility**: High abstraction risk. Mitigated by TDD for each adapter.
- **State Consistency**: Redis/Postgres divergence. Mitigated by PostgreSQL as the source of record for all state changes.
- **Volume I/O**: Podman performance bottlenecks. Mitigated by continuous performance testing in the E2E suite.

## 7. Current System State

### Services (`docker compose`)
- **coordinator** — FastAPI on 127.0.0.1:8000; runs `alembic upgrade head` on startup.
- **db** — PostgreSQL 17 + pgvector, persisted in a named volume.
- **redis** — Decision Bus.
- **dashboard** — Vite dev server on 127.0.0.1:7173, proxying `/api` to the coordinator.
- **test** — `test` profile; runs the suite against the `daash_test` database and Redis DB 15.

Networks: `backend` (internal; db, redis, coordinator, test) and `frontend` (bridge; coordinator, dashboard, published ports, LMStudio egress via `host.docker.internal`).

### API
- `GET /health`
- **Swarms**: `POST /swarms`, `GET /swarms/active`, `POST /swarms/{id}/transitions/{action}`, `DELETE /swarms/{id}/delete`. Lifecycle actions: start (CREATED → ACTIVE), pause (ACTIVE/USER_FEEDBACK → PAUSED), resume (PAUSED → ACTIVE), feedback (PAUSED → USER_FEEDBACK), verify (ACTIVE/PAUSED → VERIFICATION), learn (VERIFICATION → LEARNING), archive (VERIFICATION/LEARNING → ARCHIVED), delete (CREATED/VERIFICATION). A partial unique index guarantees a single live swarm.
- **Teams**: `GET /teams`, `GET /teams/{id}`, `GET /teams/{id}/quality` (stored DoR/DoD, null until generated), `POST /teams/{id}/quality` (optional `{dor_guidance, dod_guidance}` body) — runs the harness adapter's Quality Judge over Vision/Mission + guidance, parses strict JSON, persists both standards on the team row (regeneration overwrites), and publishes a `quality.generated` decision-bus signal. Harness errors → 502 (busy → 409); unparseable model output → 502; unknown team → 404.
- **Tasks**: `POST /tasks` (hierarchical via `parent_id`, embedded at creation), `GET /tasks?team_id=&status=`, `GET/PATCH/DELETE /tasks/{id}`, `PATCH /tasks/{id}/judgement` (cosine similarity of task text vs. team Vision + Mission embeddings; returns score, `aligned` at ≥ 0.7, and the composed Mission prompt).
- **Decision Bus**: `POST/GET /decisions` (signal feed), `POST/GET /proposals`, `GET /proposals/{id}`, `POST /proposals/{id}/votes`. Votes resolve atomically via a Lua script; all bus keys carry a TTL.

### Components
- **Embeddings** (`app/embeddings.py`): `Alibaba-NLP/gte-base-en-v1.5` (768-dim) enabled by `EMBEDDING_USE_MODEL`; deterministic 768-dim feature-hashing embedder by default.
- **Prompts** (`app/prompts.py`): `composeVisionPrompt`, `composeMissionPrompt` layer Vision → Mission → Task. `composeQualityPrompt` builds the Quality Judge prompt (Vision + Mission + optional DoR/DoD guidance, strict-JSON output contract); `extract_quality_json` parses model responses tolerantly (bare JSON, code fences, surrounding prose).
- **Adapters** (`app/adapter.py`): `BaseAdapter`, `LMStudioAdapter` (streaming), `CompositeAdapter`, `build_adapter`, `get_active_adapter`. The composite adapter is built at coordinator import time so Quality Judgement can run through it.
- **DAASHboard** (`dashboard/`): active swarm view with lifecycle action buttons, Kanban task board with status advancement, archived swarm list, and a Quality Standards panel (view/generate/regenerate team DoR/DoD with optional guidance).

### Tests
98 tests: `test_task_crud.py` (20), `test_quality.py` (20), `test_bus.py` (19), `test_swarms.py` (14), `test_embeddings.py` (9), `test_adapter.py` (9), `test_alignment.py` (7). The suite builds its schema via Alembic each session.

## 8. Completed Increments
- **2026-09-28 — Slice 1**: Alembic initial migration; hierarchical task CRUD API; resilience verified across container stop/start and full stack down/up.
- **2026-09-28 — Slice 1.5**: Embedding at task creation; `GET /teams`, `POST /swarms`; initial DAASHboard.
- **2026-09-30 — Slice 2**: Decision Bus with atomic quorum voting; consensus verified live over HTTP.
- **2026-10-01 — Slice 3**: Harness adapters; swarm lifecycle state machine and single-live-swarm migration; DAASHboard lifecycle, Kanban, and archive views.
- **2026-10-03 — Slice 4**: Vision/Mission prompt composition and Mission Judgement endpoint.
- **2026-10-03 — Infrastructure**: Internal compose network for PostgreSQL and Redis; test suite runs in the compose `test` service.
- **2026-10-03 — Slice 5 planning**: Quality Gates split into increments 5.0–5.4.
- **2026-10-03 — Slice 5.0 (Quality Judgement)**: Team DoR/DoD migration (`c4a7e2f8d1b5`), `POST/GET /teams/{id}/quality`, quality-judge prompt + tolerant JSON extraction, Quality Standards panel in DAASHboard; verified live through LMStudio with guidance honored and `quality.generated` published to the Decision Bus.
