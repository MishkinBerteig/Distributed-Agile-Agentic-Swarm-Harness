"""DAASH tools for every agent (docs/DESIGN-0.4.md section 2.1, team messages section 3).

One module builds the tools that the built-in agent's tool loop offers (app.jobs) and that the
DAASH MCP server (app.mcp_server) serves to external agents, so the two cannot drift. Each
handler gets what it needs from an `AgentContext`: repositories, the embedder, a signal function
and the swarm/team/agent/task of the job. Handlers raise `ToolError` for anything the agent
should be told about; `ToolRegistry.call` (native) and the MCP server turn that into an error
result the agent can act on.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from app import models, policy
from app.checks import validate_check_spec
from app.models import (Agent, AgentRole, CheckKind, CriterionCreate, Swarm, Task, TaskCreate,
                        Team)
from app.tools import Tool, ToolError, ToolRegistry, safe_path, workspace_tools

log = logging.getLogger("daash.agent_tools")

DELIVERY = AgentRole.MISSION_DELIVERY.value
COORD = AgentRole.SWARM_COORDINATOR.value

MESSAGE_MAX_CHARS = 4000        # one message (the agent_messages.text CHECK)
MESSAGES_PER_ATTEMPT = 20       # sends per agent per task attempt (a team message counts once)
MESSAGE_EVENT_CLIP = 200        # characters of a message shown in its `message` event
TEAM_RECIPIENT = "team"
COORDINATOR_RECIPIENT = "coordinator"
RESERVED_DIR = ".daash"         # DAASH's own folder in a swarm workspace (staging)

#: Shared tools in the order they are offered (workspace tools come first when given).
SHARED_TOOL_NAMES = ("create_subtask", "remember", "recall", "propose_team", "vote_on_proposal",
                     "list_proposals", "team_roster", "send_message", "read_messages",
                     "current_task")
WORKSPACE_TOOL_NAMES = ("list_files", "read_file", "write_file", "append_file", "delete_file")
#: Offered to the built-in agent only while a proposal of its team awaits its vote.
VOTING_TOOL_NAMES = ("vote_on_proposal", "list_proposals")

Signal = Callable[..., Awaitable[None]]
FileHook = Callable[[str, bool], Awaitable[None]]


async def no_signal(kind: str, team_id: Optional[str] = None, task_id: Optional[str] = None,
                    **payload: Any) -> None:
    return None


def bus_signal(bus: Any, timeout: float = 2.0) -> Signal:
    """A best-effort decision-bus signal function (never raises), like Jobs.signal."""
    async def signal(kind: str, team_id: Optional[str] = None, task_id: Optional[str] = None,
                     **payload: Any) -> None:
        if bus is None:
            return
        try:
            await asyncio.wait_for(bus.publish(kind, team_id, task_id, payload), timeout)
        except Exception as exc:  # noqa: BLE001
            log.debug("bus signal %s dropped: %r", kind, exc)
    return signal


@dataclass
class Repos:
    """The repositories the tools use. `from_pool` builds the real ones; tests may pass fakes
    with the same methods."""
    swarms: Any
    teams: Any
    agents: Any
    tasks: Any
    events: Any
    proposals: Any
    memories: Any
    messages: Any
    remote_jobs: Any = None
    connected: Any = None

    @classmethod
    def from_pool(cls, pool: Any) -> "Repos":
        from app.repo import (AgentRepo, ConnectedAgentRepo, EventRepo, MemoryRepo, MessageRepo,
                              ProposalRepo, RemoteJobRepo, SwarmRepo, TaskRepo, TeamRepo)
        return cls(swarms=SwarmRepo(pool), teams=TeamRepo(pool), agents=AgentRepo(pool),
                   tasks=TaskRepo(pool), events=EventRepo(pool), proposals=ProposalRepo(pool),
                   memories=MemoryRepo(pool), messages=MessageRepo(pool),
                   remote_jobs=RemoteJobRepo(pool), connected=ConnectedAgentRepo(pool))


@dataclass
class AgentContext:
    """Who is calling a tool, for which job."""
    repos: Repos
    swarm: Swarm
    team: Team
    agent: Agent
    task: Task
    embedder: Any = None
    signal: Signal = no_signal
    #: the live swarm workspace (the file tools of connected agents edit it directly)
    workspace: Optional[Path] = None
    #: the remote or connected job, when the call came over MCP
    job_id: Optional[str] = None
    #: called with (path, written) after an MCP file tool wrote or deleted a file
    on_file: Optional[FileHook] = None
    #: extra names that address a delivery agent in send_message (lowercase alias -> agent id),
    #: e.g. a connected agent's own name ("jane")
    name_aliases: dict[str, str] = field(default_factory=dict)
    #: agent id -> who that row really is, for team_roster (e.g. "Jane, connected over A2A")
    agent_labels: dict[str, str] = field(default_factory=dict)

    def embed(self, text: str) -> Optional[list[float]]:
        if self.embedder is None or not text:
            return None
        try:
            return self.embedder.embed(text)
        except Exception:  # noqa: BLE001 - embedding is an optimisation
            return None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def clip(s: Any, n: int = 300) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def sarg(args: dict, key: str, required: bool = True) -> str:
    """A string argument, stripped. An optional one may be left out ('' then); before 0.4 a
    missing optional argument (e.g. recall without `query`) was refused despite its schema."""
    v = args.get(key)
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()):
        raise ToolError(f"missing or non-string argument {key!r}")
    return v.strip()


async def settle(proposals: Any, prop: Any, n_voters: int) -> str:
    """Tally a proposal's votes; record the outcome once decided."""
    outcome = policy.tally(prop.votes, n_voters)
    if outcome != "pending":
        await proposals.set_status(prop.id, outcome)
    return outcome


def message_recipients(sender: Agent, swarm_agents: Iterable[Agent], to: str,
                       aliases: Optional[dict[str, str]] = None) -> tuple[str, list[Agent]]:
    """(audience, recipients) for send_message's `to` (docs/DESIGN-0.4.md section 3): another
    delivery agent of the sender's team by name (case-insensitive) -> "direct"; "team" -> every
    other delivery agent of the team; "coordinator" -> the Swarm Coordinator. Raises ToolError
    with the valid names otherwise."""
    agents = list(swarm_agents)
    mates = [a for a in agents if a.team_id == sender.team_id and a.role == DELIVERY
             and a.id != sender.id]
    key = " ".join((to or "").split()).lower()
    if key == TEAM_RECIPIENT:
        if not mates:
            raise ToolError("you are the only delivery agent on your team; there is nobody to "
                            "message (\"coordinator\" reaches the Swarm Coordinator)")
        return "team", mates
    if key in (COORDINATOR_RECIPIENT, "swarm coordinator"):
        coord = next((a for a in agents if a.role == COORD), None)
        if coord is None:
            raise ToolError("this swarm has no Swarm Coordinator to message")
        return "coordinator", [coord]
    if key and key == sender.name.lower():
        raise ToolError("you cannot send a message to yourself")
    by_name = {a.name.lower(): a for a in mates}
    by_id = {a.id: a for a in mates}
    hit = by_name.get(key) or by_id.get((aliases or {}).get(key, ""))
    if hit is not None:
        return "direct", [hit]
    valid = [a.name for a in mates] + [TEAM_RECIPIENT, COORDINATOR_RECIPIENT]
    raise ToolError(f"unknown recipient {to!r}; send to one of: {', '.join(valid)}")


def message_event_detail(sender: str, audience: str, recipients: Iterable[Any], text: str) -> str:
    """The `message` event: 'Delivery-1 → Delivery-2: <first 200 characters>'."""
    names = [getattr(r, "name", str(r)) for r in recipients]
    label = {"team": "team", "coordinator": "coordinator"}.get(audience) or ", ".join(names)
    return f"{sender} → {label}: {clip(text, MESSAGE_EVENT_CLIP)}"


def format_messages(messages: Iterable[Any]) -> str:
    """read_messages' reply: one line per message, oldest first."""
    lines = []
    for m in messages:
        to = "to the team" if getattr(m, "audience", "direct") == "team" else "to you"
        when = getattr(m, "created_at", None)
        stamp = f", {when:%Y-%m-%d %H:%M} UTC" if when is not None else ""
        lines.append(f"- From {getattr(m, 'from_name', None) or 'a teammate'} ({to}{stamp}): "
                     f"{getattr(m, 'text', '')}")
    return "\n".join(lines) or "(no unread messages)"


def short_args(name: str, args: dict, limit: int = 160) -> str:
    """A tool call's arguments for its `tool` event: file contents are shown by size only."""
    parts = []
    for k, v in args.items():
        if k in ("content",) and isinstance(v, str):
            parts.append(f"{k}=<{len(v)} characters>")
        elif isinstance(v, str):
            parts.append(f"{k}={clip(v, 60)!r}")
        else:
            parts.append(f"{k}={clip(repr(v), 60)}")
    return clip(", ".join(parts), limit)


def _role_title(role: str) -> str:
    from app.prompts import ROLE_TITLES
    return ROLE_TITLES.get(role, role)


def _harness_label(harness: str) -> str:
    from app.api_helpers import HARNESS_LABELS
    return HARNESS_LABELS.get(harness, harness)


_ROLE_ORDER = {AgentRole.MISSION_JUDGEMENT.value: 0, AgentRole.QUALITY_JUDGEMENT.value: 1,
               DELIVERY: 2}


def roster_text(team: Team, agents: Iterable[Agent], me: Agent,
                task_names: dict[str, str], labels: Optional[dict[str, str]] = None) -> str:
    """team_roster's reply: the team's agents with role, agent type, capabilities and state.
    `labels` names connected agents' rows (agent id -> "Jane, connected over A2A")."""
    lines = [f"Team '{team.name}'. Mission: {clip(team.mission_statement, 300)}"]
    for a in sorted(agents, key=lambda a: (_ROLE_ORDER.get(a.role, 9), a.name)):
        you = " (you)" if a.id == me.id else ""
        caps = ", ".join(a.capabilities) or "none"
        if a.state == models.AgentState.BUSY.value and a.current_task_id:
            state = f"busy on '{task_names.get(a.current_task_id, a.current_task_id)}'"
        else:
            state = a.state
        kind = (labels or {}).get(a.id) or f"agent type {_harness_label(a.harness)}"
        lines.append(f"- {a.name}{you}: {_role_title(a.role)}, {kind}, can {caps}; {state}")
    lines.append(f'Message a delivery agent by name with send_message, or "{TEAM_RECIPIENT}" '
                 f'(every other delivery agent) or "{COORDINATOR_RECIPIENT}" (the Swarm '
                 "Coordinator). The judges review your work; they do not read messages.")
    return "\n".join(lines)


def current_task_text(task: Task, job_id: Optional[str] = None) -> str:
    """current_task's reply: the task, its artifact and criteria, and the latest feedback."""
    from app.prompts import _task_block
    s = _task_block(task) + f"\nStatus: {task.status} (attempt {task.attempts + 1})"
    if task.required_capabilities:
        s += f"\nNeeds: {', '.join(task.required_capabilities)}"
    if task.feedback:
        s += f"\nLatest feedback from the judges (fix every point):\n{task.feedback.strip()}"
    if job_id:
        s += f"\nDAASH job: {job_id}"
    return s


_PROTOCOL_WORDS = {"a2a": "over A2A", "hermes-api": "through the Hermes API",
                   "runner": "on an agent host"}


async def connected_names(repos: Repos, swarm_id: str, team_id: Optional[str] = None
                          ) -> tuple[dict[str, str], dict[str, str]]:
    """(lowercase alias -> agent id, agent id -> roster label) for the swarm's delivery rows that
    are connected agents (docs/DESIGN-0.4.md section 5), so teammates can message "Jane" and
    team_roster says who Delivery-2 really is. Aliases are for `team_id`'s rows (messages stay
    inside a team); a name that fills several rows of that team is a label but not an alias
    (it would be ambiguous)."""
    if repos.connected is None:
        return {}, {}
    rows = [a for a in await repos.agents.list(swarm_id)
            if a.harness == models.CONNECTED_HARNESS and a.provider]
    if not rows:
        return {}, {}
    aliases: dict[str, str] = {}
    labels: dict[str, str] = {}
    seen: dict[tuple[Optional[str], str], int] = {}
    cache: dict[str, Any] = {}
    for a in rows:
        if a.provider not in cache:
            cache[a.provider] = await repos.connected.get(a.provider)
        ca = cache[a.provider]
        if ca is None:
            continue
        labels[a.id] = (f"{ca.name}, a connected agent "
                        f"{_PROTOCOL_WORDS.get(ca.protocol, ca.protocol)}")
        key = (a.team_id, ca.name.lower())
        seen[key] = seen.get(key, 0) + 1
    for a in rows:
        ca = cache.get(a.provider)
        if (ca is not None and (team_id is None or a.team_id == team_id)
                and seen.get((a.team_id, ca.name.lower())) == 1):
            aliases.setdefault(ca.name.lower(), a.id)
    return aliases, labels


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

def _obj(props: dict, req: Iterable[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(req)}


_S = {"type": "string"}


def build_agent_tools(ctx: Optional[AgentContext], *, workspace: bool) -> list[Tool]:
    """DAASH's tools for one agent's job. `workspace=True` adds the file tools, which edit the
    live swarm workspace (connected agents only; the built-in agent has its own, and runner CLIs
    work on a copy). `ctx=None` gives the specs only (the handlers must not be called)."""
    tools = _workspace_tools(ctx) if workspace else []

    def c() -> AgentContext:
        if ctx is None:
            raise ToolError("this tool list has no job attached")
        return ctx

    async def create_subtask(args: dict) -> str:
        x = c()
        name = sarg(args, "name")
        tasks = await x.repos.tasks.list(team_id=x.team.id)
        if policy.task_budget_left(tasks, x.swarm.config.limits.max_tasks_per_team) <= 0:
            raise ToolError("the team has used its whole task budget; no more tasks")
        artifact = sarg(args, "artifact", required=False)
        crits = [CriterionCreate(text=t.strip()) for t in (args.get("acceptance_criteria") or [])
                 if isinstance(t, str) and t.strip()]
        if artifact:
            if validate_check_spec(CheckKind.FILE_EXISTS.value, artifact, None):
                raise ToolError(f"invalid artifact path {artifact!r}")
            crits.append(CriterionCreate(text=f"{artifact} exists",
                                         check_kind=CheckKind.FILE_EXISTS, check_path=artifact))
        if not crits:
            raise ToolError("give at least one acceptance criterion (or an artifact path)")
        sub = await x.repos.tasks.create(
            x.swarm.id, TaskCreate(team_id=x.team.id, name=name,
                                   description=sarg(args, "description", required=False),
                                   artifact=artifact, parent_id=x.task.id,
                                   required_capabilities=models.required_capabilities(
                                       args.get("required_capabilities")),
                                   acceptance_criteria=crits),
            created_by=x.agent.id, embedding=x.embed(name))
        await x.repos.events.add(
            x.swarm.id, "created", team_id=x.team.id, task_id=sub.id, actor=x.agent.id,
            detail=f"{x.agent.name} created subtask '{name}' under '{x.task.name}'.")
        return f"created subtask {sub.id} (Pending; Mission Judgement will prioritise it)"

    async def remember(args: dict) -> str:
        x = c()
        content = sarg(args, "content")
        await x.repos.memories.add(x.agent.id, x.swarm.id, content, team_id=x.team.id,
                                   task_id=x.task.id, embedding=x.embed(content))
        return "remembered"

    async def recall(args: dict) -> str:
        x = c()
        query = sarg(args, "query", required=False)
        mems = await x.repos.memories.search(x.agent.id, x.embed(query), k=5)
        return "\n".join(f"- {m.content}" for m in mems) or "(no memories yet)"

    async def delivery_agents(x: AgentContext) -> list[Agent]:
        return [a for a in await x.repos.agents.list(x.swarm.id, x.team.id) if a.role == DELIVERY]

    async def propose_team(args: dict) -> str:
        x = c()
        name = sarg(args, "name")
        mission = sarg(args, "mission_statement")
        reason = sarg(args, "reason", required=False)
        teams = await x.repos.teams.list(x.swarm.id)
        if len(teams) >= x.swarm.config.limits.max_teams:
            raise ToolError(f"the swarm already has the maximum of "
                            f"{x.swarm.config.limits.max_teams} teams")
        wanted = name.lower()
        if any(t.name.lower() == wanted for t in teams) or any(
                p.name.lower() == wanted and p.status in ("pending", "approved")
                for p in await x.repos.proposals.list(x.swarm.id)):
            raise ToolError(f"a team or proposal named {name!r} already exists")
        voters = await delivery_agents(x)
        prop = await x.repos.proposals.create(
            x.swarm.id, x.team.id, x.agent.id, name, mission, reason,
            policy.majority_quorum(len(voters)))
        prop = await x.repos.proposals.record_vote(prop.id, x.agent.id, True) or prop
        outcome = await settle(x.repos.proposals, prop, len(voters))
        await x.repos.events.add(
            x.swarm.id, "proposal", team_id=x.team.id, actor=x.agent.id,
            detail=f"{x.agent.name} proposed a new team '{name}' "
                   f"({reason or 'no reason given'}); vote status: {outcome}.")
        await x.signal("proposal.created", x.team.id, x.task.id, proposal_id=prop.id)
        return f"proposal {prop.id} created; status {outcome}"

    async def vote_on_proposal(args: dict) -> str:
        x = c()
        pid = sarg(args, "proposal_id")
        approve = args.get("approve")
        if not isinstance(approve, bool):
            raise ToolError("'approve' must be true or false")
        prop = await x.repos.proposals.get(pid)
        if prop is None or prop.swarm_id != x.swarm.id or prop.status != "pending":
            raise ToolError("no such pending proposal")
        if prop.team_id != x.team.id:
            raise ToolError("your team cannot vote on that proposal")
        prop = await x.repos.proposals.record_vote(pid, x.agent.id, approve) or prop
        voters = await delivery_agents(x)
        outcome = await settle(x.repos.proposals, prop, len(voters))
        await x.repos.events.add(
            x.swarm.id, "proposal", team_id=x.team.id, actor=x.agent.id,
            detail=f"{x.agent.name} voted {'for' if approve else 'against'} the proposal for "
                   f"team '{prop.name}'; vote status: {outcome}.")
        return f"vote recorded; status {outcome}"

    async def list_proposals(args: dict) -> str:
        x = c()
        props = await x.repos.proposals.list(x.swarm.id, status="pending")
        mine = [p for p in props if p.team_id == x.team.id and x.agent.id not in p.votes]
        if not mine:
            return "(no pending proposals awaiting your vote)"
        lines = []
        for p in mine:
            yes = sum(1 for v in p.votes.values() if v)
            lines.append(f"- {p.id}: new team '{p.name}': {clip(p.mission_statement, 300)} "
                         f"(reason: {clip(p.reason, 200) or 'none given'}; votes so far: "
                         f"{yes} for, {len(p.votes) - yes} against)")
        return "\n".join(lines) + "\nVote with vote_on_proposal(proposal_id, approve)."

    async def team_roster(args: dict) -> str:
        x = c()
        agents = await x.repos.agents.list(x.swarm.id, x.team.id)
        names = {t.id: t.name for t in await x.repos.tasks.list(team_id=x.team.id)}
        return roster_text(x.team, agents, x.agent, names, x.agent_labels)

    async def send_message(args: dict) -> str:
        x = c()
        # Live run 2026-11-13: OpenClaw first sent `message=` and then left out `to`. Both are clear enough to
        # accept: `message` is another name for `text`, and no `to` means the whole team.
        to = sarg(args, "to", required=False) or TEAM_RECIPIENT
        text = sarg(args, "text" if args.get("text") is not None or args.get("message") is None
                    else "message")
        if len(text) > MESSAGE_MAX_CHARS:
            raise ToolError(f"the message is {len(text)} characters long; the limit is "
                            f"{MESSAGE_MAX_CHARS}")
        audience, recipients = message_recipients(
            x.agent, await x.repos.agents.list(x.swarm.id), to, x.name_aliases)
        sent = await x.repos.messages.sends_in_attempt(x.agent.id, x.task.id, x.task.attempts)
        if sent >= MESSAGES_PER_ATTEMPT:
            raise ToolError(f"you have sent {MESSAGES_PER_ATTEMPT} messages in this attempt, the "
                            "limit; keep working on the task")
        await x.repos.messages.send(x.swarm.id, x.team.id, x.agent.id,
                                    [r.id for r in recipients], text, audience=audience,
                                    task_id=x.task.id, attempt=x.task.attempts)
        await x.repos.events.add(
            x.swarm.id, "message", team_id=x.team.id, task_id=x.task.id, actor=x.agent.id,
            detail=message_event_detail(x.agent.name, audience, recipients, text))
        if audience == "team":
            return (f"message sent to {len(recipients)} team member(s): "
                    + ", ".join(r.name for r in recipients))
        if audience == "coordinator":
            return "message sent to the Swarm Coordinator"
        return f"message sent to {recipients[0].name}"

    async def read_messages(args: dict) -> str:
        x = c()
        return format_messages(await x.repos.messages.take_unread(x.agent.id))

    async def current_task(args: dict) -> str:
        x = c()
        task = await x.repos.tasks.get(x.task.id) or x.task
        return current_task_text(task, x.job_id)

    tools += [
        Tool("create_subtask", "Split off a smaller task under the current one. It starts "
             "Pending until Mission Judgement approves it.",
             _obj({"name": _S, "description": _S, "artifact": {**_S, "description":
                   "workspace path the subtask produces"},
                   "acceptance_criteria": {"type": "array", "items": _S},
                   "required_capabilities": {"type": "array", "items": {
                       "type": "string", "enum": ["run_code", "web_research"]}}}, ["name"]),
             create_subtask),
        Tool("remember", "Save a short note to your persistent memory.",
             _obj({"content": _S}, ["content"]), remember),
        Tool("recall", "Search your persistent memory.", _obj({"query": _S}), recall),
        Tool("propose_team", "Propose spinning out a new team for work outside this team's "
             "mission. Your team's delivery agents vote (majority wins).",
             _obj({"name": _S, "mission_statement": _S, "reason": _S},
                  ["name", "mission_statement"]), propose_team),
        Tool("vote_on_proposal", "Vote on a pending team proposal from your team.",
             _obj({"proposal_id": _S, "approve": {"type": "boolean"}},
                  ["proposal_id", "approve"]), vote_on_proposal),
        Tool("list_proposals", "List your team's pending proposals for new teams that you have "
             "not voted on yet.", _obj({}), list_proposals),
        Tool("team_roster", "List your team's agents: name, role, agent type, capabilities, "
             "state and current task.", _obj({}), team_roster),
        Tool("send_message", "Send a short message (at most 4000 characters) to a delivery "
             "agent of your team by name (for example Delivery-2), to \"team\" (every other "
             "delivery agent of your team) or to \"coordinator\" (the Swarm Coordinator).",
             _obj({"to": {**_S, "description": 'a teammate\'s name, "team" (the default) or '
                                               '"coordinator"'},
                   "text": _S}, ["text"]), send_message),
        Tool("read_messages", "Read your unread team messages, oldest first; they are then "
             "marked read.", _obj({}), read_messages),
        Tool("current_task", "Show your current task: its artifact, acceptance criteria and "
             "the latest feedback from the judges.", _obj({}), current_task),
    ]
    return tools


def submit_tool(submitted: dict[str, str]) -> Tool:
    """The built-in agent's terminal tool (native only: remote and connected agents submit by
    finishing, their final reply being the summary)."""
    async def submit(args: dict) -> str:
        submitted["summary"] = sarg(args, "summary", required=False)
        return "submitted for review"

    return Tool("submit", "Finish: submit your work for review with a short summary of what you "
                "did and how each acceptance criterion is met.",
                _obj({"summary": _S}, ["summary"]), submit, terminal=True)


def _reserved(rel: str) -> bool:
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    return bool(parts) and parts[0] == RESERVED_DIR


def _workspace_tools(ctx: Optional[AgentContext]) -> list[Tool]:
    """The native file tools on the live swarm workspace (same path rules and size limits),
    keeping DAASH's `.daash/` folder out of reach and reporting each written or deleted path
    through `ctx.on_file` (the deliverable's files)."""
    root = (ctx.workspace if ctx is not None and ctx.workspace is not None else Path("."))
    registry = ToolRegistry()
    base = {t.name: t for t in workspace_tools(root, registry)}
    resolved_root = Path(root).resolve()

    def rel_of(args: dict) -> str:
        path = args.get("path")
        if path is None:
            return "."
        target = safe_path(root, path)
        return target.relative_to(resolved_root).as_posix()

    def wrap(name: str) -> Tool:
        inner = base[name]

        async def handler(args: dict) -> str:
            if ctx is None or ctx.workspace is None:
                raise ToolError("this tool list has no workspace attached")
            rel = rel_of(args)
            if _reserved(rel):
                raise ToolError(f"{RESERVED_DIR}/ is reserved for DAASH")
            out = await inner.handler(args)
            if name == "list_files":
                out = "\n".join(line for line in out.splitlines()
                                if not line.startswith(RESERVED_DIR + "/")) or "(empty)"
            elif ctx.on_file is not None and name in ("write_file", "append_file", "delete_file"):
                await ctx.on_file(rel, name != "delete_file")
            return out

        return Tool(inner.name, inner.description, inner.parameters, handler)

    return [wrap(n) for n in WORKSPACE_TOOL_NAMES]


def tool_specs(*, workspace: bool, native: bool = False) -> list[dict]:
    """OpenAI-format specs of the tools, without a job (help pages, MCP tools/list)."""
    reg = ToolRegistry()
    for t in build_agent_tools(None, workspace=workspace):
        reg.add(t)
    if native:
        reg.add(submit_tool({}))
    return reg.specs()
