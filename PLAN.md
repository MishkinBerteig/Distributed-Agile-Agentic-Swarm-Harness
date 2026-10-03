# Implementation Plan: Distributed Agile Agentic Swarm Harness (DAASH)

## 1. System Overview
DAASH is a containerized harness for managing distributed swarms of AI Agents organized into teams. The system ensures alignment through a hierarchical prompt structure (Vision $\rightarrow$ Mission) and maintains strict quality/mission gates.

### Core Constraints
- **Containerization**: Must support Docker and Podman.
- **Persistence**: Full system state must be resilient to container shutdown/restart.
- **Scale**: Designed for high token volume (10M+ context, 100M+ thinking tokens).
- **Visibility**: Human-readable global task database with specific performance metrics (p90 cycle time, error rates).

## 2. Proposed Technology Stack
- **Language**: Python (FastAPI for coordination API, Pydantic for data validation).
- **Database ORM**: SQLAlchemy 2.0 (Async) for structured data and migrations.
- **Migrations**: Alembic.
- **Vector Store**: pgvector (integrated via SQLAlchemy) for RAG and task searchability.
- **Message Broker**: Redis (for inter-agent decision-making and high-frequency signals).
- **Persistence**: Persistent Docker/Podman volumes mapping to the DB storage.
- **Containerization**: Multi-stage Dockerfiles (compatible with Podman) and `docker-compose.yaml` / `podman-compose.yaml`.
- **Supported Harnesses**: Adapters for OpenClaw, Hermes Agent, OpenClaude, Claude Code, Codex, and OpenWebUI.

## 3. Architectural Components

### A. Swarm Coordinator
Top-level orchestrator managing the Vision Statement, team lifecycle, and resource budgets.

### B. Team Structure
Teams composed of Mission Judgement (Alignment), Quality Judgement (DoD Audit), and Mission Delivery Agents.

### C. Task Coordination System (The "Ground Truth")
Global database for:
- **Task State**: Pending $\rightarrow$ Ready (per DoR) $\rightarrow$ In-Progress $\rightarrow$ Done.
- **Communication**: The primary record for all non-decision communication and deliverables.
- **Metrics**: p90 cycle time and failure rate tracking.

### D. Communication & Memory
- **Decision Bus (Redis)**: Rapid, ephemeral signals for coordination/voting.
- **Persistent Memory**: RAG (vector store), Audit Trail (transcripts), and Task History.

## 4. Extreme Programming (XP) Execution Model

DAASH will be developed using a strict XP approach, abandoning phase-based milestones in favor of **Incremental Delivery** and **Continuous Integration**.

### A. Testing & Quality Requirements (The "Definition of Done")
No feature is considered complete until it meets the following testing rigor:
1.  **Unit Testing (TDD)**: 
    - Every logic change starts with a failing test.
    - High coverage of business logic, validators, and adapters.
2.  **E2E Testing**: 
    - Full container orchestration tests (Podman/Docker).
    - Verification of state resilience: `container stop` $\rightarrow$ `container start` $\rightarrow$ verify zero data loss.
3.  **Security Testing**: 
    - Input validation for all agent-submitted data (preventing prompt injection into the coordinator).
    - Secure volume mounting and container isolation checks.
4.  **Performance Testing**: 
    - Stress testing the Task DB under high concurrency of agent updates.
    - Measuring latency of Redis decision bus vs. PostgreSQL task updates.

### B. Architectural Consequences
- **Refactoring as a First-Class Citizen**: The architecture will evolve through continuous refactoring. If a design choice hinders the current story's implementation, it is refactored immediately.
- **Simple Design**: Implement the simplest thing that works for the current test case; avoid speculative abstractions.
- **Small Releases**: Every completed story (e.g., "Ability to persist a single task across restart") is deployed and verified in the harness immediately.

## 5. Incremental Story Backlog (Prioritized)
*Instead of phases, we execute these as independent, testable slices:*

- [x] **Slice 1: Durable Task State** $\rightarrow$ SQLAlchemy models (Tasks/Teams) with pgvector + Alembic migrations + Volume persistence + Unit tests for hierarchical task CRUD.
  - *Status 2026-09-28: Complete — Alembic initial migration is the sole schema path; resilience E2E verified. DAASHboard tracked under Slice 1.5.*
- [x] **Slice 1.5: Simple web-based UI in React "DAASHboard"** to allow a human to inspect current state of tasks and teams and "create" a swarm (minimal in this slice) + embedding column.
- [x] **Slice 2: The Decision Bus** $\rightarrow$ Redis integration + voting logic tests + E2E "consensus" test + updated DAASHboard.
  - *Status 2026-09-30: Complete — ephemeral signal feed + atomic quorum voting over Redis, consensus verified live; user smoke-tested.*
- [x] **Slice 3: Harness Adapters** $\rightarrow$ Adapter interface + LMStudio adapter + `POST /agents/run` endpoint + E2E integration test + updated DAASHboard (clickable tasks + AgentRun component).
  - *Status 2026-10-01: Complete — 61 tests (52 logic + 9 adapter), Docker builds, smoke-tested against live LMStudio.*
- [ ] **Slice 4: Alignment Hierarchy** $\rightarrow$ Vision/Mission prompt injection + Mission Judgement logic tests.
- [ ] **Slice 5: Quality Gates** $\rightarrow$ DoD/DoR validation logic + E2E "Task Rejection" flow + updated DAASHboard.
- [ ] **Slice 6: Audit & RAG** $\rightarrow$ Session transcript storage + vector retrieval tests.
- [ ] **Slice 7: Observability** $\rightarrow$ Metrics engine + updated DAASHboard.

## 6. Risk Assessment
- **Harness Compatibility**: High abstraction risk. Mitigated by TDD for each adapter.
- **State Consistency**: Redis/Postgres divergence risk. Mitigated by making Postgres the definitive source for all state changes.
- **Volume I/O**: Podman performance bottlenecks. Mitigated by continuous performance testing in the E2E suite.

## 7. Progress Log

### 2026-09-28 — Increment 1 foundation functional (Slice 1 partially green)

**Current API surface (coordinator):** `GET /health`, `POST /tasks` (auto-creates referenced team), `GET /tasks/{id}`, `PATCH /tasks/{id}` (status enum: Pending | Ready | In-Progress | Done), `DELETE /tasks/{id}` → 204.

**Verified:** `.venv/bin/python -m pytest` → 12 passed; `docker compose up --build -d` bootstraps a fresh pgvector DB from ORM and serves traffic; full task create/read/update/delete smoke test passes against the live containers (user-confirmed).

**Remaining for Slice 1 completion:**
- [x] Alembic initial migration generated + applied in entrypoint (migration path, not just `db_setup` fallback).
- [x] Volume persistence/resilience E2E: stop container → start → data intact.
- [x] Hierarchical tasks (parent/child) exercised end-to-end via API.
- [x] Embedding column populate at task creation. *(pulled earlier from Slice 6 and tracked in Slice 1.5)*
- [x] Initial DAASHboard. *(tracked in Slice 1.5)*

### 2026-09-28 — Increment 2: Slice 1 complete

**Added/changed:**
- **Alembic initial migration** (`migrations/versions/9ce40a1bd3c6_initial_schema_teams_tasks_memory_.py`) — autogenerate shows zero drift vs `app/orm.py`; includes `CREATE EXTENSION vector`. `migrations/env.py` now honors `DAASH_ALEMBIC_DATABASE_URL`/`DAASH_DATABASE_URL`, so the old alembic.ini-rewrite hack and the `db_setup` fallback are gone (`app/db_setup.py` deleted; entrypoint runs `alembic upgrade head` as the only schema path).
- **API**: `GET /tasks?team_id=&status=` implemented (was a stub); `PATCH` persists `acceptance_criteria` and rejects invalid statuses via the `TaskStatus` enum (422); `POST /tasks` accepts `parent_id`, returns 422 for unknown parents. Note: FK violation ordering is non-deterministic, so parent existence is checked up front rather than inferred from constraint names alone.
- **Tests** (TDD, red→green): +7 tests → `.venv/bin/python -m pytest` = **19 passed**. `tests/conftest.py` now builds the test schema with `alembic upgrade head` (session fixture), so every test run exercises the migration; task rows truncate between tests.

**E2E verified on live containers (`docker compose`, pgvector:pg17):**
- Fresh bootstrap: `down -v && up --build` → empty volume bootstrapped purely via Alembic, `/health` OK.
- Hierarchy via API: parent + child created over HTTP; list shows correct `parent_id` links; PATCH updated status/acceptance_criteria.
- Resilience: `coordinator stop/start` with API confirmed down in between → data intact; full stack `down` (volumes kept) `&& up -d` → zero loss, migration idempotent (no re-run).

**Next:** Slice 1.5 DAASHboard & embedding column.

### 2026-09-28 — Increment 3: Slice 1.5 (embedding at creation + initial DAASHboard)

**Added/changed:**
- **Embeddings** (`app/embeddings.py`): primary model `Alibaba-NLP/gte-base-en-v1.5` (768-dim, matches existing `Vector(768)` columns; user directive), selected via `DAASH_EMBEDDING_MODEL`. Lazy resolution on first embed so startup never blocks on a ~400MB download; if sentence-transformers or the model is unavailable it falls back to a deterministic L2-normalized feature-hashing embedder (also 768-dim) and logs — embedding rows are always populated. `EMBEDDING_USE_MODEL` (default **false**) gates real-model use; `sentence-transformers` added as optional extra `[embeddings]`. **Deviation:** the real model is not exercised in this environment yet (no torch install); plumbing + fallback are fully tested, flipping the flag exercises gte-base later without schema/API change.
- **Embed at task creation**: `POST /tasks` composes name+description+acceptance_criteria and embeds off-loop (`asyncio.to_thread`); `TaskRepository.create(embedding=...)` inserts `$n::vector`. Reads switched from `SELECT *` to an explicit column list so asyncpg (no vector codec registered) never fetches the column.
- **API for the board**: `GET /teams`, `POST /swarms` ({name, vision_statement, mission_statement} → team row), CORS wide open for dev.
- **DAASHboard** (`dashboard/`): Vite + React 18 — swarm list w/ selection, create-swarm form, per-swarm task tree (parent/child via `parent_id`), status badges + filter, add-task form (exercises embedding server-side). API base defaults to `/api`; dev proxy → coordinator. Compose service `dashboard` (node:26-alpine, port 5173, named node_modules volume).
- **Tests**: +13 → **32 passed** (`tests/test_embeddings.py`, `tests/test_swarms.py`).

**E2E verified on live containers:** POST /swarms → swarm row; POST /tasks → `vector_dims(embedding)=768` in DB; browser (playwright) at :5173: created "E2E Swarm" via form, selected it, added task via form, task renders nested with badge and its row has a 768-dim vector; `/api` proxy works through Vite.

**Next:** Slice 2 (Decision Bus).

### 2026-09-30 — Increment 4: Slice 2 (Decision Bus — Redis)

**Current state:**
- **Decision Bus** (`app/bus.py`): Redis-backed ephemeral signal feed + quorum voting. Signals pushed via `lpush` with `ltrim` cap (`BUS_SIGNALS_MAX`, default 200); every key TTL'd (`BUS_TTL_SECONDS`, default 3600s), so the bus self-heals and PostgreSQL remains the source of record. Proposals stored as Redis hashes under `daash:proposal:*`; approve/reject voter tallies are SETs in a separate `daash:votes:*` namespace (also TTL'd); proposal listing tolerates any non-hash keys it scans. **Atomic voting** via Lua script — duplicate voters and post-decision votes refused; first side to reach quorum resolves exactly once under parallel load. Lifecycle `pending → approved | rejected`.
- **API endpoints**: `POST /decisions`, `GET /decisions`, `POST /proposals`, `GET /proposals?team_id=`, `GET /proposals/{id}`, `POST /proposals/{id}/votes` (404 unknown proposal, 409 duplicate/already-decided). Vote resolution auto-publishes `proposal_vote` + `proposal_approved`/`proposal_rejected` signals to the feed.
- **Tests**: `tests/test_bus.py` — **19 tests** covering signal ordering + cap; voting logic (quorum approve/reject, quorum-1, duplicate voter, post-decision vote, unknown proposal); listing with existing votes, legacy stray keys, and tally TTLs; parallel-consensus races (concurrent approves, split approve/reject race, racing same voter); TTL ephemerality; full E2E consensus over the HTTP API + error-code mapping. Redis fixture uses DB 15, flushed per test, isolated from the live dev bus.
- **DAASHboard**: stacked tab nav under the header — "Overview" (swarms/tasks) and "Decision Bus". The Decision Bus tab shows a live signal feed with status-colored badges, a proposals list (counts + quorum, click to open), a create-proposal form, and a voting booth (Voter ID + Approve/Reject). Every error banner includes a plain-language explanation of the cause and remedy.
- Verified: full suite **51 passed**; consensus flow verified live over HTTP on `docker compose` containers; dashboard production build clean.

**Next:** Slice 3 (Harness Adapters — adapter interface + first harness + E2E integration test + DAASHboard).
