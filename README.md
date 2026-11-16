# Distributed Agile Agentic Swarm Harness (DAASH)

Objective: an opinionated low-friction, high-reliability containerized harness
for managing a distributed swarm of teams of hetrogenous AI Agents. A "swarm" is
at least one team of AI Agents that have persistent memory across sessions, a
shared task coordination and tracking system, and a long-running goal that will
require a minimum of 10M context tokens, 100M thinking tokens, and produce 1M
tokens of durable output.

## Alignment: Vision Statement

The swarm stays aligned by all AI agents in the swarm having an identical vision
statement embedded in a system prompt for all sessions. The vision statement is
provided upon setup of a swarm and is inserted deterministically into every
session.

## Structure: Teams

The swarm is composed of teams of AI agents, possibly with hetrogenous agentic
harnesses and underlying LLMs. A team of AI agents shares a common ongoing
purpose that contributes towards the vision of the swarm. Once created, a team
exists until the overall vision is accomplished. An existing team may be re-
purposed at any time if the conditions of its creation are either satisfied or
invalidated.

## Sub-Alignment: Mission Statements

Each team in the swarm gets a mission statement which describes either an
operational or project-based contribution to the vision of the swarm. That
misison statement is embedded into the system prompt for all sessions for the
AI agents in the team, is supplied by the creator of the team, and must directly
refer to either the vision or the mission of the creator of the team.

## Team Composition

Every team in a swarm has at least three AI agents which function as follows:
- The "Mission Judgement" agent monitors the work of the other AI agents in the
team to ensure that they are aligned with the mission and to check if the team
has accomplished the mission, or if there is a failure which requires learning
and restarting.
- The "Quality Judgement" agent monitors the work of the other AI agents in the
team to ensure that they are meeting ongoing quality expectations including the
definition of done.
- One or more "Mission Delivery" agents which work on tasks to accomplish the
mission of the team, which divide work among themselves, and which decide by
majority vote if a new team with a new mission needs to be created in order to
accomplish some portion of the mission.

## Tasks

A task is a piece of work which has a beginning and a scope defined by one or
more acceptance criteria that support a team's mission. A task must result in
a change to the form, fit or function of an artifact that contributes to the
advancement of the vision of the swarm. Tasks are decomposable into smaller
tasks which may be any combination of sequential and parallel. A task must be
testable such that each acceptance criteria can be met (green) or not (red)
with certainty. Acceptance criteria are unique to every task. A team may CRUD
its own tasks using any decision mechanism within that team.

## Visibility

The entire swarm uses a single evolving task database to coordinate and create
a visible means of measuring progress towards the vision. Measurement must
include:
- p90 cycle time for tasks from commitment to done
- error rates per wall clock interval
- team failure rates per task
The task database must be accessible to and readable by humans.

## Commitment Policy: Definition of Ready

A team may not begin working on a task until that team meets a definition of
ready which minimally includes that at least one Mission Delivery agent is
idle AND the team has no in-progress tasks that are over the trailing p90
cycle time of the last 100 tasks for the swarm AND the Mission Judgement
has approved the task as being the next highest priority to accomplishing
the mission of the team.

## Definition of Done

The definition of done has two levels: the vision level and the team mission
level. Every Mission Delivery agent must produce work that meets both levels of
the definition of done. The definition of done minimally includes that the
Mission Judgement agent validates that the expected value of the mission is
met with 99% confidence or better AND that the Quality Judgement agent has
approved 100% quality score for all deterministic quality checks and >99.9%
quality score for all statistical and rubric-based quality checks. Every task
worked upon by the same team shares the same definition of done.

## Learning

The swarm learns through three mechanisms:
1. Noticing and sharing patterns that improve value, quality or speed.
2. Automating work with scripts that are deterministic, fast and cost fewer
tokens.
3. Building classifiers to allocate work to the least-time|token-expensive
agent/LLM combination required to get that work done.

## Configuration

A swarm has an allowed list of agent harnesses, LLM providers and LLM models
and a "Swarm Coordinator" agent harness, LLM provider and LLM model. The
Swarm Coordinator takes the vision statement for the swarm and manages the
swarm to achieve the vision. The configuration also includes resource limits
optionally stated in wall-clock time, token budgets, maximum simultaneous
agents, budget and core "Values and Principles" set at a global level.

## Status: 0.4.1

DAASH 0.4.1 (tag `v0.4.1`) runs a swarm as described above, from the vision
to verified artifacts. [PLAN.md](PLAN.md) has the plan, the history of each
milestone and what comes next. Releases are on
[GitHub](https://github.com/MishkinBerteig/Distributed-Agile-Agentic-Swarm-Harness/releases).

What is built, section by section:
- **Alignment:** the vision, the global Values and Principles, each team's
  mission, and the Definitions of Ready and Done go into every agent's system
  prompt, word for word, in every session.
- **Teams:** the user names the teams when creating a swarm, or the Swarm
  Coordinator forms them from the vision. Every team has a Mission Judgement,
  a Quality Judgement and one or more Mission Delivery agents. Delivery agents
  propose new teams and decide by majority vote.
- **Tasks:** a task moves Pending → Ready → In-Progress → Review → Done, or
  ends Failed or Rejected. Each acceptance criterion is green or red. Code
  checks the deterministic ones: a file exists, a file contains a pattern, a
  minimum word count, valid JSON, and a command that must succeed (run by a
  runner). Quality Judgement scores the rubric ones. Delivery agents split
  tasks into subtasks.
- **Definitions of Ready and Done:** enforced in code (`app/policy.py`) with
  the thresholds above: deterministic checks 100%, rubric checks > 99.9%,
  mission confidence ≥ 99%.
- **Visibility:** PostgreSQL is the single task database. DAASHboard shows
  the Kanban, each task's criteria, verdicts and timeline, the artifacts, an
  activity feed, and the metrics: p90 cycle time, errors per interval and
  team failure rates.
- **Learning:** (1) learnings and each agent's own memories go into its
  prompts, chosen by meaning (see Search below); (2) automation scripts are
  recorded, not yet run; (3) a classifier
  rates each task's complexity to pick its starting model, and routing moves
  up a model ladder (`instant → fast → great → amazing` by default) on
  failures and on learned success rates.
- **Configuration:** allowed harnesses and models, a model per role
  (including the Swarm Coordinator's), and limits: tokens, wall-clock time,
  USD budget, simultaneous agents, teams, tasks per team and attempts per
  task. Values and Principles are global (DAASHboard Settings).
- **Heterogeneous agents:**
  - the built-in `daash` harness, which edits files;
  - OpenClaude, Claude Code, Codex, OpenClaw and Hermes Agent, run by DAASH
    runners, which edit files and run code;
  - existing agents connected to DAASH: OpenClaw over A2A, Hermes Agent
    through its API server, or either through an agent host on a machine that
    only connects outbound. DAASH finds them on the network, and the agent's
    owner approves the connection with one step.
- **DAASH's tools for every agent:** one MCP server (agent port 8001, `/mcp`)
  gives every agent, built in or external, the same tools: memory (`remember`,
  `recall`), subtasks, team proposals and votes, the team roster, team
  messages, and its current task.
- **Search:** an embedding model turns every task, learning and memory into a
  vector. PostgreSQL (pgvector) stores each vector with the name of its model
  and indexes the model's vectors (HNSW). Each job's prompt gets the learnings
  and the agent's memories nearest in meaning to the job's subject, and the
  `recall` tool searches an agent's memories the same way. The model runs in
  the `embedder` service. The built-in model, `gte-base-en-v1.5`, is in its
  image and needs no network. DAASHboard Settings › Search embeddings chooses
  another model, and the embedder then downloads it and embeds everything
  again.

The swarm lifecycle is in `Swarm State Diagram.mermaid` and is drawn live in
DAASHboard. One swarm is live at a time.

## Run it

Everything runs in Docker.

```bash
cp .env.example .env            # set DAASH_LLM_API_KEY, and DAASH_LLM_ENDPOINT for your gateway
docker compose up --build -d    # db, redis, coordinator :8000, agent-api :8001, engine, embedder, dashboard :7173
```

The first build takes a few minutes: the embedder image (about 3 GB) holds
PyTorch and the built-in embedding model.

- DAASHboard: http://127.0.0.1:7173. Its menu has Swarm, Past swarms, Agents &
  models, Settings and Help.
- API docs: http://127.0.0.1:8000/docs
- The LLM endpoint is any OpenAI-compatible gateway. The default model names
  (`instant`, `fast`, `great`, `amazing`) are aliases on that gateway.

Optional:

```bash
# agent CLIs in a container (set DAASH_RUNNER_BOOTSTRAP_TOKEN in .env first)
docker compose --profile runner up --build -d runner
```

- Runners on other machines: [docs/RUNNER.md](docs/RUNNER.md).
- Connecting existing agents, and agent hosts:
  [docs/AGENT-HOSTS.md](docs/AGENT-HOSTS.md) and DAASHboard's Help.
- Agents on other machines reach DAASH on port 8001. `DAASH_AGENT_BIND_ADDR`,
  `DAASH_AGENT_PUBLIC_URL` and `DAASH_DISCOVERY_HOSTS` in `.env` set this up
  (see `.env.example`).
- Admin auth is off unless `DAASH_API_TOKEN` is set. The coordinator is
  published on 127.0.0.1 only by default.
- Embedding models other than the built-in one are downloaded from Hugging
  Face into the `daash-models` volume. A gated model also needs `HF_TOKEN` in
  `.env`.

## Tests

Three tiers, all in Docker, all with a scripted fake LLM
([docs/DESIGN-0.2.md](docs/DESIGN-0.2.md)):

```bash
docker compose --profile test run --rm --build test                # unit, seconds
docker compose --profile integration run --rm --build integration  # real PostgreSQL and Redis, about a minute
scripts/e2e.sh                                                     # real containers, a few minutes
```

The unit and integration tiers embed with word hashes, in-process. The e2e
tier uses the real embedder with its built-in model, on a network with no way
out.

## Documentation

- [PLAN.md](PLAN.md): the plan, current status and what comes next.
- [docs/DESIGN-0.1.md](docs/DESIGN-0.1.md) to
  [docs/DESIGN-0.4.md](docs/DESIGN-0.4.md): the design of each milestone, and
  [docs/DESIGN-0.4.1.md](docs/DESIGN-0.4.1.md): real embeddings for search.
- [docs/RUNNER.md](docs/RUNNER.md): runners for agent CLIs.
- [docs/AGENT-HOSTS.md](docs/AGENT-HOSTS.md): agent hosts.
- DAASHboard Help: the prompts guide (rendered live from the code) and
  connecting agents.

## Known limitations (0.4.1)

- The built-in harness only edits files. Running code comes from the runner
  harnesses and command checks. Automation scripts are recorded, not run.
- Judges are LLMs: their verdicts are good, not infallible. Only a command
  check proves that tests pass.
- Tasks are embedded and indexed, but nothing searches them yet: search
  covers learnings and memories.
- Right after the embedding model changes, items the embedder has not
  re-embedded yet are found by recency, not meaning, until it has.
- Connected agents use their own tools. An agent on an agent host is told to
  stay in its job folder but is not confined to it, and command checks for its
  jobs run on that machine as its owner.
- DAASHboard polls for updates. No harness offers web research by default, so
  research tasks run without it.
