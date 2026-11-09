# DAASH 0.2 — Design Addendum

0.2 = **(A) a three-tier test harness** + **(B) throughput and convergence fixes** found in the 0.1 live runs.
Everything in docs/DESIGN-0.1.md still holds unless changed here.

## A. Test harness

The harness is a first-class part of DAASH. It must make swarm behaviour reproducible without the real LLM
gateway, and stay fast enough to run on every change.

| Tier | What | Command | Budget |
|---|---|---|---|
| unit | pure logic, parsers, sandbox, mocked HTTP (unchanged) | `docker compose --profile test run --rm --build test` | < 2 s |
| integration | real PostgreSQL + Redis; API in-process (ASGI); engine in-process; scripted fake LLM in-process | `docker compose --profile integration run --rm --build integration` | < 90 s |
| e2e | the real containers (coordinator, engine, db, redis) + a fake-LLM container, driven over HTTP | `scripts/e2e.sh` | < 6 min |

### A.1 Scripted fake LLM (`tests/fakes/`)
* `fake_llm.py`: `FakeLLM` is a deterministic, OpenAI-compatible responder. `handle(request: dict) -> dict` reads
  the system prompt's `# Your Role:` and the user message's `Job:` line to tell which DAASH job is asking, then answers
  with contract-valid JSON (or tool calls for work tasks: `write_file` content that satisfies the task's
  deterministic criteria, then `submit`). It uses only the public prompt contracts, never engine internals.
* **Scenarios** (`configure(dict)`, also via HTTP `POST /control/scenario`): number of teams and tasks to plan, review
  failures on the first N attempts per task, an outage window (503s for N seconds), malformed-JSON replies for the
  next N calls, an "accomplished" confidence, slow replies (delay ms), and vote approve/reject. `reset()` restores
  the defaults (happy path).
* **Recording**: every call is recorded (time, model, job, role, tokens) for assertions (`GET /control/calls`).
* **Transports**: in-process via `httpx.MockTransport(fake.httpx_handler)` passed to `LLMClient(transport=...)`;
  over the network via `fake_llm_server.py` (`python -m tests.fakes.fake_llm_server --port 9100`), serving
  `/v1/models`, `/v1/chat/completions`, `/control/*`.
* **Prioritize answers**: include both `next_task_ids` (list, B.1) and `next_task_id` (first of the list), so the
  answer is valid under both the 0.1 and 0.2 contracts.

### A.2 Integration tier (`tests/integration/`, replaces the 0.1-obsolete tests)
Compose service `integration` (profile `integration`) on the backend network. It depends on db and redis, uses
database `daash_test` (dropped and re-migrated per session with `alembic upgrade head`) and Redis DB 15. Covers:
* migrations from empty and from the pre-0.1 head; every repository method; `load_snapshot`;
* every API endpoint and status code (httpx `ASGITransport`, real DB, fake LLM for `/teams/{id}/quality`);
* **the engine in-process**: build `Engine`, inject `Jobs` with an `LLMClient` on the fake transport, loop
  `tick()` + await `engine.running` until the swarm reaches VERIFICATION, asserting the state after each phase
  (bootstrap, standards, plan, prioritize, commit, work, review, rework, Done, assess, complete). Also:
  restart semantics (a new `Engine` instance mid-run resets BUSY agents and resumes without duplicate work), user
  overrides during jobs, limits pause plus resume, proposals (vote/expire/materialize), dead-dependency stall,
  outage cooldown (no attempt counted), parse failure escalation, two delivery agents working concurrently.

### A.3 E2E tier (`tests/e2e/`, `docker-compose.e2e.yaml`, `scripts/e2e.sh`)
A separate compose project (`-p daash-e2e`, **no published ports**, so it can run beside the dev stack):
db, redis, coordinator, engine (`DAASH_LLM_ENDPOINT=http://fake-llm:9100`, `DAASH_ENGINE_TICK_SECONDS=0.5`),
`fake-llm` (test image), and `e2e` (test image running `pytest tests/e2e`, talking to `http://coordinator:8000` and
`http://fake-llm:9100`, with `/var/run/docker.sock` mounted so it can restart the engine container through the
Docker Engine API with httpx over the unix socket; no docker CLI needed). `scripts/e2e.sh` runs
`up --build --abort-on-container-exit --exit-code-from e2e`, then `down -v` regardless of the result.
Scenarios:
* happy path to VERIFICATION, with metrics and artifacts;
* rework then Done;
* gateway outage cool-down and recovery;
* **engine container restart mid-run** (completes, no lost or duplicated tasks);
* pause stops LLM spending (no fake-LLM calls while PAUSED), then resume completes;
* token-limit pause;
* user reject mid-run;
* learn → retrospective → archive.

## B. Throughput and convergence

* **B.1 Batch approval.** Each team keeps a buffer of approved work equal to its number of delivery agents.
  `decide()` emits PRIORITIZE when the team's Ready-with-dependencies-done count is below its delivery-agent count
  and a Pending candidate with dependencies done exists. The prioritize contract gains `next_task_ids` (up to
  `slots` = agents − ready). `parse_prioritize(obj, valid_ids, slots=1)` still accepts the legacy single
  `next_task_id`. The engine approves all listed tasks.
* **B.2 Judges don't serialize.** Per-agent locks apply only to Mission Delivery agents (they own a task and its
  workspace edits). Judges and the coordinator may run concurrent jobs. An agent is BUSY while at least one of its
  jobs runs (reference count).
* **B.3 Per-model concurrency.** `LLMClient(..., default_concurrency=None, model_concurrency=None)` holds a
  semaphore per model around each HTTP call. Settings: `DAASH_LLM_MAX_CONCURRENCY` (default 4) and
  `DAASH_LLM_MODEL_CONCURRENCY` (JSON, e.g. `{"amazing": 1}`). One LLM server saturates above ~4 concurrent calls.
* **B.4 Leaving ACTIVE stops spending.** When the live swarm is not ACTIVE (and not LEARNING for the retrospective),
  the engine cancels its running jobs on the next tick. Cancellation resets agents to IDLE and records the usage
  spent so far best-effort. A job never writes results unless the swarm is still ACTIVE (LEARNING for the
  retrospective). On resume, `decide()` re-dispatches In-Progress/Review tasks, which are idempotent. Entering
  VERIFICATION (user `verify` or COMPLETE_SWARM) closes all open tasks as Rejected with reason "Closed: the swarm
  went to verification before this task finished." This is done by one function, `services.close_open_tasks`.
* **B.5 Convergence.** Already in place (DESIGN-0.1 §12): blocking verdicts and assessments must name concrete
  gaps. The integration tier verifies the re-ask path with the fake LLM.
* **B.6 Deferral (post-tag fix, found in the First Garden re-run).** A team whose work needs another team's
  artifacts (e.g. a care checklist that cross-references the guides) had its tasks rejected one by one
  ("Definition of Ready not met"), re-planned, and used up its task budget, because the prioritize contract forced
  an approval. Now Mission Judgement may answer `"next_task_ids": []`: the candidates stay Pending, a `deferred`
  event is recorded, and `jobs.prioritize` raises `jobs.Deferred(done_count)`. The engine holds that job key
  (`engine.Deferrals`) until the swarm's Done-task count changes or `DEFER_SECONDS` (300 s) pass, so there is no
  hot loop. The prompt says a task is never rejected only because it cannot start yet, and lists the files that
  exist in the shared workspace (all teams) so the judge can see whether a dependency is met. A missing
  `next_task_ids` still falls back to approving the first candidates.
  Deferral is offered only while work can still change something (`policy.work_in_flight`: a non-stalled task
  is Ready/In-Progress/Review anywhere, or another ACTIVE team will plan or be assessed). Otherwise the prompt
  says waiting cannot help, and an empty answer falls back to approving the first candidate; the judge must
  approve or reject. (Found in the same run: a refinement task was deferred after every other team had
  finished, which would have re-asked every 5 minutes forever.)
