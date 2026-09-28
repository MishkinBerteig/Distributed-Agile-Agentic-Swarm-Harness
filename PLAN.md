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

- [ ] **Slice 1: Durable Task State** $\rightarrow$ SQLAlchemy models (Tasks/Teams) with pgvector + Alembic migrations + Volume persistence + Unit tests for hierarchical task CRUD.
  - *Status 2026-09-28: API + schema + tests working; Alembic intentionally at zero (see Progress Log).*
- [ ] **Slice 1.5: Simple web-based UI in React "DAASHboard" to allow a human to inspect current state of tasks and teams and "create" a swarm (minimal in this slice).
- [ ] **Slice 2: The Decision Bus** $\rightarrow$ Redis integration + voting logic tests + E2E "consensus" test + updated DAASHboard.
- [ ] **Slice 3: Harness Adapters** $\rightarrow$ Adapter interface + implementation for the first harness (e.g., OpenClaude) + E2E integration test + updated DAASHboard.
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
- [ ] Alembic initial migration generated + applied in entrypoint (migration path, not just `db_setup` fallback).
- [ ] Volume persistence/resilience E2E: stop container → start → data intact.
- [ ] Hierarchical tasks (parent/child) exercised end-to-end via API.
- [ ] Embedding column currently unused — wire into Slice 6 (Audit & RAG) or populate at task creation.
- [ ] Initial DAASHboard.
