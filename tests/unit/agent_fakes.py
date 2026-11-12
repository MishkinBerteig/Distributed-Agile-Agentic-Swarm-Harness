"""In-memory stand-ins for the repositories DAASH's agent tools and MCP server use
(app.agent_tools.Repos), so the unit tier can exercise them without a database. Only the
methods the tools call are implemented, with the same signatures and return types."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app import models
from app.agent_tools import AgentContext, Repos
from app.models import (Agent, AgentMessage, ConnectedAgent, Memory, RemoteJob, Swarm, Task,
                        TaskEvent, Team, TeamProposal)

T0 = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)


def _id() -> str:
    return str(uuid.uuid4())


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


CLOCK = Clock()


def make_swarm(state: str = "ACTIVE", sid: str = "s1") -> Swarm:
    return Swarm(id=sid, name="Primes", vision_statement="Ship primes.py", lifecycle_state=state,
                 created_at=T0)


def make_team(tid: str = "tm1", name: str = "Primes", sid: str = "s1") -> Team:
    return Team(id=tid, swarm_id=sid, name=name, mission_statement="Build primes.py and tests.",
                created_at=T0)


def make_agent(aid: str, name: str, role: str = "mission_delivery", team_id: Optional[str] = "tm1",
               harness: str = "daash", state: str = "idle",
               current_task_id: Optional[str] = None) -> Agent:
    return Agent(id=aid, swarm_id="s1", team_id=team_id, role=role, name=name, harness=harness,
                 capabilities=models.harness_capabilities(harness), model="fast", state=state,
                 current_task_id=current_task_id, created_at=T0)


def make_task(tid: str = "t1", name: str = "Implement primes", status: str = "In-Progress",
              attempts: int = 0, feedback: Optional[str] = None, team_id: str = "tm1") -> Task:
    crit = models.AcceptanceCriterion(id="c1", task_id=tid, ordinal=1, text="primes.py exists",
                                      check_kind="file_exists", check_path="primes.py")
    return Task(id=tid, swarm_id="s1", team_id=team_id, name=name, artifact="primes.py",
                status=status, attempts=attempts, feedback=feedback, criteria=[crit],
                created_at=T0, updated_at=T0)


class FakeSwarms:
    def __init__(self, *swarms: Swarm) -> None:
        self.by_id = {s.id: s for s in swarms}

    async def get(self, sid: str) -> Optional[Swarm]:
        return self.by_id.get(sid)


class FakeTeams:
    def __init__(self, *teams: Team) -> None:
        self.by_id = {t.id: t for t in teams}

    async def get(self, tid: str) -> Optional[Team]:
        return self.by_id.get(tid)

    async def list(self, swarm_id: str) -> list[Team]:
        return [t for t in self.by_id.values() if t.swarm_id == swarm_id]


class FakeAgents:
    def __init__(self, *agents: Agent) -> None:
        self.by_id = {a.id: a for a in agents}

    async def get(self, aid: str) -> Optional[Agent]:
        return self.by_id.get(aid)

    async def list(self, swarm_id: str, team_id: Optional[str] = None) -> list[Agent]:
        return [a for a in self.by_id.values() if a.swarm_id == swarm_id
                and (team_id is None or a.team_id == team_id)]


class FakeTasks:
    def __init__(self, *tasks: Task) -> None:
        self.by_id = {t.id: t for t in tasks}

    async def get(self, tid: str) -> Optional[Task]:
        return self.by_id.get(tid)

    async def list(self, swarm_id: Optional[str] = None, team_id: Optional[str] = None,
                   status: Optional[str] = None) -> list[Task]:
        return [t for t in self.by_id.values()
                if (swarm_id is None or t.swarm_id == swarm_id)
                and (team_id is None or t.team_id == team_id)
                and (status is None or t.status == status)]

    async def create(self, swarm_id: str, data: models.TaskCreate, created_by: str = "user",
                     embedding: Any = None) -> Task:
        t = Task(id=_id(), swarm_id=swarm_id, team_id=data.team_id, name=data.name,
                 description=data.description, artifact=data.artifact, parent_id=data.parent_id,
                 required_capabilities=list(data.required_capabilities), created_by=created_by,
                 created_at=T0, updated_at=T0)
        self.by_id[t.id] = t
        return t


class FakeEvents:
    def __init__(self) -> None:
        self.events: list[TaskEvent] = []

    async def add(self, swarm_id: str, kind: str, detail: str = "", team_id: Optional[str] = None,
                  task_id: Optional[str] = None, from_status: Optional[str] = None,
                  to_status: Optional[str] = None, actor: str = "engine") -> TaskEvent:
        e = TaskEvent(id=len(self.events) + 1, swarm_id=swarm_id, team_id=team_id,
                      task_id=task_id, kind=kind, from_status=from_status, to_status=to_status,
                      actor=actor, detail=detail, created_at=CLOCK())
        self.events.append(e)
        return e

    def of(self, kind: str) -> list[TaskEvent]:
        return [e for e in self.events if e.kind == kind]


class FakeMemories:
    def __init__(self) -> None:
        self.rows: list[Memory] = []

    async def add(self, agent_id: str, swarm_id: str, content: str, team_id: Optional[str] = None,
                  task_id: Optional[str] = None, embedding: Any = None) -> Memory:
        m = Memory(id=_id(), agent_id=agent_id, swarm_id=swarm_id, team_id=team_id,
                   task_id=task_id, content=content, created_at=CLOCK())
        self.rows.append(m)
        return m

    async def search(self, agent_id: str, embedding: Any, k: int = 8) -> list[Memory]:
        return [m for m in reversed(self.rows) if m.agent_id == agent_id][:k]


class FakeProposals:
    def __init__(self, *props: TeamProposal) -> None:
        self.by_id = {p.id: p for p in props}

    async def list(self, swarm_id: str, status: Optional[str] = None) -> list[TeamProposal]:
        return [p for p in self.by_id.values() if p.swarm_id == swarm_id
                and (status is None or p.status == status)]

    async def get(self, pid: str) -> Optional[TeamProposal]:
        return self.by_id.get(pid)

    async def create(self, swarm_id, team_id, proposer, name, mission, reason, quorum):
        p = TeamProposal(id=_id(), swarm_id=swarm_id, team_id=team_id, proposer_agent_id=proposer,
                         name=name, mission_statement=mission, reason=reason, quorum=quorum,
                         created_at=T0)
        self.by_id[p.id] = p
        return p

    async def record_vote(self, pid: str, agent_id: str, approve: bool):
        p = self.by_id[pid]
        p = p.model_copy(update={"votes": {**p.votes, agent_id: approve}})
        self.by_id[pid] = p
        return p

    async def set_status(self, pid: str, status: str, created_team_id: Optional[str] = None):
        p = self.by_id[pid].model_copy(update={"status": status})
        self.by_id[pid] = p
        return p


class FakeMessages:
    def __init__(self, agents: FakeAgents, teams: FakeTeams) -> None:
        self.rows: list[AgentMessage] = []
        self._agents, self._teams = agents, teams

    def _named(self, m: AgentMessage) -> AgentMessage:
        sender = self._agents.by_id.get(m.from_agent_id)
        to = self._agents.by_id.get(m.to_agent_id or "")
        team = self._teams.by_id.get(sender.team_id) if sender and sender.team_id else None
        return m.model_copy(update={"from_name": sender.name if sender else None,
                                    "from_team": team.name if team else None,
                                    "to_name": to.name if to else None})

    async def send(self, swarm_id, team_id, from_agent_id, to_agent_ids: Iterable[str], text,
                   audience="direct", task_id=None, attempt=None) -> list[AgentMessage]:
        send_id = _id()
        now = CLOCK()
        out = []
        for to in dict.fromkeys(to_agent_ids):
            m = AgentMessage(id=_id(), send_id=send_id, swarm_id=swarm_id, team_id=team_id,
                             from_agent_id=from_agent_id, to_agent_id=to, audience=audience,
                             task_id=task_id, attempt=attempt, text=text, created_at=now)
            self.rows.append(m)
            out.append(self._named(m))
        return out

    async def unread(self, agent_id: str, limit: int = 30) -> list[AgentMessage]:
        return [self._named(m) for m in self.rows
                if m.to_agent_id == agent_id and m.read_at is None][:limit]

    async def mark_read(self, ids: Iterable[str]) -> int:
        ids = set(ids)
        n = 0
        for i, m in enumerate(self.rows):
            if m.id in ids and m.read_at is None:
                self.rows[i] = m.model_copy(update={"read_at": CLOCK()})
                n += 1
        return n

    async def take_unread(self, agent_id: str, limit: int = 30) -> list[AgentMessage]:
        msgs = await self.unread(agent_id, limit)
        await self.mark_read(m.id for m in msgs)
        return msgs

    async def sends_in_attempt(self, from_agent_id, task_id, attempt) -> int:
        return len({m.send_id for m in self.rows if m.from_agent_id == from_agent_id
                    and m.task_id == task_id and m.attempt == attempt})


class FakeRemoteJobs:
    def __init__(self, *jobs: RemoteJob) -> None:
        self.by_id = {j.id: j for j in jobs}
        self.hashes: dict[str, str] = {}   # token hash -> job id

    async def get(self, jid: str) -> Optional[RemoteJob]:
        return self.by_id.get(jid)

    async def by_mcp_token_hash(self, h: str) -> Optional[RemoteJob]:
        jid = self.hashes.get(h)
        return self.by_id.get(jid) if jid else None

    async def active_connected(self, connected_id: str) -> list[RemoteJob]:
        return [j for j in self.by_id.values() if j.harness == "connected"
                and j.status in ("queued", "leased")
                and j.payload.get("connected_agent_id") == connected_id]

    async def count_mcp_call(self, jid: str) -> None:
        j = self.by_id[jid]
        self.by_id[jid] = j.model_copy(update={"payload": {
            **j.payload, "mcp_calls": int(j.payload.get("mcp_calls", 0)) + 1}})

    async def record_mcp_file(self, jid: str, path: str, written: bool = True) -> None:
        j = self.by_id[jid]
        files = [f for f in j.payload.get("mcp_files", []) if f != path]
        if written:
            files.append(path)
        self.by_id[jid] = j.model_copy(update={"payload": {**j.payload, "mcp_files": files}})

    def set_status(self, jid: str, status: str) -> None:
        self.by_id[jid] = self.by_id[jid].model_copy(update={"status": status})


class FakeConnected:
    def __init__(self, *agents: ConnectedAgent) -> None:
        self.by_id = {a.id: a for a in agents}
        self.seen: list[str] = []

    async def by_mcp_token_hash(self, h: str) -> Optional[ConnectedAgent]:
        return next((a for a in self.by_id.values() if a.mcp_token_hash == h), None)

    async def get(self, aid: str) -> Optional[ConnectedAgent]:
        return self.by_id.get(aid)

    async def touch_mcp(self, aid: str) -> None:
        self.seen.append(aid)


class World:
    """A swarm with one team: Mission Judgement, Quality Judgement, Delivery-1..3 and the Swarm
    Coordinator; Delivery-1 works on task t1."""

    def __init__(self, state: str = "ACTIVE", task_status: str = "In-Progress") -> None:
        self.swarm = make_swarm(state)
        self.team = make_team()
        self.other_team = make_team("tm2", "Docs")
        self.d1 = make_agent("a-d1", "Delivery-1", state="busy", current_task_id="t1")
        self.d2 = make_agent("a-d2", "Delivery-2", harness="codex")
        self.d3 = make_agent("a-d3", "Delivery-3")
        self.mj = make_agent("a-mj", "Mission Judgement", role="mission_judgement")
        self.qj = make_agent("a-qj", "Quality Judgement", role="quality_judgement")
        self.coord = make_agent("a-co", "Swarm Coordinator", role="swarm_coordinator",
                                team_id=None)
        self.other = make_agent("a-o1", "Delivery-1", team_id="tm2")
        self.task = make_task(status=task_status)
        agents = FakeAgents(self.d1, self.d2, self.d3, self.mj, self.qj, self.coord, self.other)
        teams = FakeTeams(self.team, self.other_team)
        self.repos = Repos(swarms=FakeSwarms(self.swarm), teams=teams, agents=agents,
                           tasks=FakeTasks(self.task), events=FakeEvents(),
                           proposals=FakeProposals(), memories=FakeMemories(),
                           messages=FakeMessages(agents, teams), remote_jobs=FakeRemoteJobs(),
                           connected=FakeConnected())

    def ctx(self, agent: Optional[Agent] = None, **kw: Any) -> AgentContext:
        return AgentContext(repos=self.repos, swarm=self.swarm, team=self.team,
                            agent=agent or self.d1, task=self.task, **kw)

    def job(self, jid: str = "job-1", status: str = "leased", harness: str = "codex",
            agent: Optional[Agent] = None, **payload: Any) -> RemoteJob:
        j = RemoteJob(id=jid, swarm_id="s1", team_id="tm1", task_id=self.task.id,
                      agent_id=(agent or self.d1).id, attempt=0, harness=harness, model="fast",
                      status=status, payload=dict(payload), created_at=T0)
        self.repos.remote_jobs.by_id[jid] = j
        return j
