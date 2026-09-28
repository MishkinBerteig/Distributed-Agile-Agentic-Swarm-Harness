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
