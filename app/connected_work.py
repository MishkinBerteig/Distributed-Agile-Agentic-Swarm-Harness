"""Work on connected agents (docs/DESIGN-0.4.md sections 5.5, 5.6 and 5.9 "Work"): the engine side
of a delivery agent row that is an existing agent elsewhere.

`ConnectedWork` is a mixin of `app.jobs.Jobs` (it uses its repositories, `busy` state, events and
`_attempt_failed`). Two ways:
* direct (`a2a`, `hermes-api`): DAASH sends the agent one message and polls until it finishes.
  The agent edits the live workspace through DAASH's MCP file tools or hands files over as reply
  blocks. The job is a `remote_jobs` row (harness "connected", no runner) that runners never lease;
  its MCP calls work while it is queued.
* through an agent host (`runner`): a runner installed next to the agent leases the job (pinned by
  `payload.runner_id`), relays it to the local agent and uploads the result like any runner job.
"""
from __future__ import annotations

import asyncio
import logging
import types
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app import agent_tools, connectors, policy, prompts, remote, services
from app.connectors import ConnectorError
from app.llm import Usage
from app.models import (CONNECTED_HARNESS, Agent, ConnectedAgent, Swarm, SwarmStatus, Task,
                        TaskStatus, Team)

log = logging.getLogger("daash.connected")

#: The model recorded for a connected agent's calls (DAASH does not choose or see its model).
CONNECTED_CALL_MODEL = "connected"
PROTOCOL_LABELS = {"a2a": "A2A", "hermes-api": "the Hermes API", "runner": "an agent host"}
#: The runner relay adapter for an agent-host agent, by kind (section 5.9).
RELAYS = {"openclaw": "openclaw-agent", "hermes": "hermes-agent"}
#: Excerpts of the task's own files in a connected work message.
EXCERPT_FILES = 4
EXCERPT_CHARS = 8000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def via(ca: ConnectedAgent) -> str:
    """'Jane, via A2A' / 'Fitz, via an agent host'."""
    return f"{ca.name}, via {PROTOCOL_LABELS.get(ca.protocol, ca.protocol)}"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(v: Any) -> Optional[datetime]:
    if not isinstance(v, str) or not v:
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class ConnectedWork:
    """Mixin for app.jobs.Jobs."""

    #: httpx transport for connector calls (tests pass an httpx.MockTransport)
    connector_transport: Any = None

    # -- helpers ------------------------------------------------------------------------------

    async def connected_names(self, swarm_id: str, team_id: Optional[str] = None
                              ) -> tuple[dict[str, str], dict[str, str]]:
        """(alias -> agent id, agent id -> label) for the swarm's connected agent rows, so
        teammates can message "Jane" and the roster shows who Delivery-2 really is."""
        return await agent_tools.connected_names(self.repos, swarm_id, team_id)

    async def _connected_agent(self, agent: Agent) -> Optional[ConnectedAgent]:
        if agent.harness != CONNECTED_HARNESS or not agent.provider:
            return None
        return await self.repos.connected.get(agent.provider)

    async def _connected_capacity_filter(self, swarm: Swarm, idle: list[Agent]) -> list[Agent]:
        """Idle agents without the rows of connected agents already at their max_jobs: one
        connected agent may fill several rows, but its max_jobs caps its concurrent work
        (section 5.2)."""
        rows = [a for a in idle if a.harness == CONNECTED_HARNESS and a.provider]
        if not rows:
            return idle
        agents = await self.agents.list(swarm.id)
        tasks = await self.tasks.list(swarm_id=swarm.id)
        load = policy.connected_load(agents, tasks)
        caps: dict[str, int] = {}
        for cid in {a.provider for a in rows}:
            ca = await self.repos.connected.get(cid)
            caps[cid] = ca.max_jobs if ca is not None else 1
        return [a for a in idle if not (a.harness == CONNECTED_HARNESS and a.provider
                                        and load.get(a.provider, 0) >= caps.get(a.provider, 1))]

    async def _handover_if_connected(self, swarm: Swarm, team: Team, task: Task) -> bool:
        """Escalation by handover (section 5.6): DAASH does not pick a connected agent's model, so
        when its attempt fails the task goes to the least recently active other capable idle
        delivery agent of the team; with none, the same agent tries again."""
        fresh = await self.tasks.get(task.id)
        if fresh is None or not fresh.assigned_agent_id:
            return False
        agents = await self.agents.list(swarm.id, team.id)
        current = next((a for a in agents if a.id == fresh.assigned_agent_id), None)
        if current is None or current.harness != CONNECTED_HARNESS:
            return False
        tasks = await self.tasks.list(team_id=team.id)
        new = policy.handover_target(current, fresh, agents, tasks)
        if new is None:
            return False
        ca = await self.repos.connected.get(current.provider) if current.provider else None
        name = ca.name if ca is not None else current.name
        await self.tasks.update(task.id, assigned_agent_id=new.id)
        await self.events.add(
            swarm.id, "info", team_id=team.id, task_id=task.id, actor="engine",
            detail=f"{name} could not finish '{fresh.name}'; handing it to {new.name}.")
        return True

    # -- the attempt --------------------------------------------------------------------------

    async def _connected_work(self, swarm: Swarm, team: Team, agent: Agent, task: Task) -> bool:
        """One attempt on a connected agent. False = run it on the built-in agent instead."""
        ca = await self._connected_agent(agent)
        if ca is None or not ca.enabled:
            why = "was removed" if ca is None else f"({ca.name}) is disabled"
            await self.events.add(
                swarm.id, "info", team_id=team.id, task_id=task.id, actor=agent.id,
                detail=f"{agent.name}'s connected agent {why}, so {agent.name} ran "
                       f"'{task.name}' on the built-in agent instead.")
            return False
        if not await self._wait_for_capacity(swarm, team, agent, task, ca):
            return False
        if ca.protocol == "runner":
            return await self._agent_host_work(swarm, team, agent, task, ca)
        return await self._direct_work(swarm, team, agent, task, ca)

    async def _wait_for_capacity(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                                 ca: ConnectedAgent) -> bool:
        """Wait while the connected agent already has max_jobs other active DAASH jobs; give up
        (built-in agent) after remote_fallback_minutes."""
        started = _now()
        while True:
            active = [j for j in await self.repos.remote_jobs.active_connected(ca.id)
                      if j.task_id != task.id]
            if len(active) < max(1, ca.max_jobs):
                return True
            wait = swarm.config.remote_fallback_minutes
            if wait is not None and _now() - started > timedelta(minutes=wait):
                await self.events.add(
                    swarm.id, "info", team_id=team.id, task_id=task.id, actor=agent.id,
                    detail=f"{ca.name} stayed busy with other DAASH work for {wait} minutes, so "
                           f"{agent.name} ran '{task.name}' on the built-in agent instead.")
                return False
            await asyncio.sleep(self.connected_poll_seconds)

    async def _agent_host_work(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                               ca: ConnectedAgent) -> bool:
        """A `runner` connected agent: its agent host's runner leases the job (pinned to it),
        relays it to the local agent and uploads the result; then as remote work."""
        system = await services.agent_system_prompt(
            self.pool, swarm, agent, team, query=f"{task.name}. {task.description}",
            embedder=self.embedder)
        messages = await self.messages.unread(agent.id)
        prompt = prompts.remote_work_prompt(
            task, task.criteria, task.feedback,
            await asyncio.to_thread(_listing, self.workspace(swarm)),
            capabilities=agent.capabilities, messages=messages)
        payload = self._remote_payload(swarm, task, system, prompt)
        payload.update({"connected_agent_id": ca.id, "protocol": "runner",
                        "runner_id": ca.runner_id, "relay": RELAYS.get(ca.kind, "openclaw-agent"),
                        "local_ref": ca.local_ref})

        def no_pickup(wait: int) -> str:
            return (f"{ca.name}'s agent host did not pick up '{task.name}' within {wait} minutes "
                    f"(is its runner online?), so {agent.name} ran it on the built-in agent "
                    f"instead.")

        label = {"who": f"{agent.name} ({ca.name}, on agent host '{{runner}}')",
                 "deliverable": {"connected_agent": ca.name, "connected_agent_id": ca.id,
                                 "protocol": "runner"},
                 "call_model": CONNECTED_CALL_MODEL}
        return await self._run_remote_job(swarm, team, agent, task, "", CONNECTED_HARNESS,
                                          payload, messages, no_pickup, label)

    async def _direct_work(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                           ca: ConnectedAgent) -> bool:
        repo = self.repos.remote_jobs
        session = connectors.context_id(swarm.id, task.id, task.attempts)
        payload = {"connected_agent_id": ca.id, "protocol": ca.protocol, "context_id": session,
                   "required_capabilities": list(task.required_capabilities),
                   "task": {"name": task.name, "artifact": task.artifact}}
        job = await repo.ensure_active(swarm.id, team.id, task.id, agent.id, task.attempts,
                                       CONNECTED_HARNESS, "", payload)
        if (job.harness != CONNECTED_HARNESS or job.attempt != task.attempts
                or (job.payload or {}).get("connected_agent_id") != ca.id):
            await repo.cancel(job.id)       # an older job of this task (another agent/attempt)
            job = await repo.ensure_active(swarm.id, team.id, task.id, agent.id, task.attempts,
                                           CONNECTED_HARNESS, "", payload)
        job_id = job.id
        try:
            if not job.payload.get("external_id"):
                handled = await self._send_connected(swarm, team, agent, task, ca, job)
                if handled is not None:
                    return handled
            return await self._follow_connected(swarm, team, agent, task, ca, job_id)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(self._cancel_connected_unless_active(ca, job_id, swarm.id))
            except BaseException as exc:  # noqa: BLE001 - best effort
                log.warning("could not cancel connected job %s: %r", job_id, exc)
            raise

    async def _send_connected(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                              ca: ConnectedAgent, job) -> Optional[bool]:
        """Build the message and send it. None = sent (follow it); True/False = handled."""
        repo = self.repos.remote_jobs
        ws = self.workspace(swarm)
        system = await services.agent_system_prompt(
            self.pool, swarm, agent, team, query=f"{task.name}. {task.description}",
            embedder=self.embedder)
        unread = await self.messages.unread(agent.id)
        listing = await asyncio.to_thread(_listing, ws)
        excerpts = await asyncio.to_thread(_excerpts, ws, task)
        text, carried = prompts.connected_work_prompt(
            system, task, task.criteria, task.feedback, listing, excerpts, unread,
            job_id=job.id, capabilities=agent.capabilities)
        await repo.merge_payload(job.id, {"prompt": text})
        try:
            progress = await connectors.start_work(ca, text, job.payload["context_id"],
                                                   message_id=f"daash-{job.id}",
                                                   transport=self.connector_transport)
        except ConnectorError as exc:
            return await self._connected_trouble(swarm, team, agent, task, ca, job.id, exc,
                                                 sent=False)
        await self.messages.mark_read(m.id for m in carried)
        await repo.merge_payload(job.id, {"external_id": progress.external_id,
                                          "sent_at": _iso(_now()), "error_since": None,
                                          "messages": [m.id for m in carried]})
        await self.repos.connected.record_check(ca.id, True)
        await self.events.add(
            swarm.id, "info", team_id=team.id, task_id=task.id, actor=agent.id,
            detail=f"{agent.name} handed '{task.name}' to {ca.name} (via "
                   f"{PROTOCOL_LABELS.get(ca.protocol, ca.protocol)}); waiting for its reply.")
        if progress.state != "working":
            return await self._finish_connected(swarm, team, agent, task, ca, job.id, progress)
        return None

    async def _follow_connected(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                                ca: ConnectedAgent, job_id: str) -> bool:
        """Poll the agent until it finishes, the time limit passes or the task moves on."""
        repo = self.repos.remote_jobs
        limit = timedelta(minutes=swarm.config.limits.remote_job_timeout_minutes)
        while True:
            job = await repo.get(job_id)
            if job is None:
                raise agent_error("the connected job disappeared")
            if job.status in ("cancelled", "abandoned", "completed"):
                await self._require_state(swarm)
                raise agent_error(f"the connected job was {job.status} by the coordinator")
            if await self._still(task.id, TaskStatus.IN_PROGRESS.value) is None:
                await self._stop_connected(ca, job)
                log.info("connected work on %r dropped: the task changed", task.name)
                return True
            sent = _parse_iso(job.payload.get("sent_at")) or job.created_at
            if _now() - sent > limit:
                await self._stop_connected(ca, job)
                await self._attempt_failed(
                    swarm, team, agent, task, "", types.SimpleNamespace(
                        files_written=list(job.payload.get("mcp_files") or [])),
                    f"{agent.name} ({via(ca)}) ran out of time.", remote=True)
                return True
            try:
                progress = await connectors.poll_work(ca, job.payload["external_id"],
                                                      transport=self.connector_transport)
            except ConnectorError as exc:
                if exc.kind == "not_found":
                    # e.g. the OpenClaw gateway restarted and forgot its tasks: send again as
                    # a new job; no attempt is counted
                    await repo.abandon(job.id)
                    await self.messages.mark_unread(job.payload.get("messages") or [])
                    await self.events.add(
                        swarm.id, "info", team_id=team.id, task_id=task.id, actor=agent.id,
                        detail=f"{ca.name} no longer knows '{task.name}' (its gateway may have "
                               f"restarted); DAASH will send it again.")
                    raise agent_error(f"{ca.name} lost the task ({exc})")
                return await self._connected_trouble(swarm, team, agent, task, ca, job.id, exc,
                                                     sent=True)
            if job.payload.get("error_since"):
                await repo.merge_payload(job.id, {"error_since": None})
                await self.repos.connected.record_check(ca.id, True)
            if progress.state != "working":
                return await self._finish_connected(swarm, team, agent, task, ca, job.id,
                                                    progress)
            await asyncio.sleep(await self._poll_interval(ca))

    async def _poll_interval(self, ca: ConnectedAgent) -> float:
        """connected_poll_seconds per active job of the agent: OpenClaw allows 30 requests a
        minute per peer across all of its tasks, so several jobs poll more slowly."""
        active = len(await self.repos.remote_jobs.active_connected(ca.id))
        return self.connected_poll_seconds * max(1, active)

    async def _connected_trouble(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                                 ca: ConnectedAgent, job_id: str, exc: ConnectorError,
                                 sent: bool) -> bool:
        """An infrastructure problem talking to the agent: `last_error`, then RemoteError
        (cooldown, no attempt counted), or after remote_fallback_minutes of trouble the attempt
        runs on the built-in agent. A JSON-RPC refusal is handled the same way: it says nothing
        about the work (most likely a protocol mismatch), so it must not use up attempts."""
        repo = self.repos.remote_jobs
        await self.repos.connected.record_check(ca.id, False, exc.stored())
        job = await repo.get(job_id)
        now = _now()
        since = _parse_iso((job.payload if job else {}).get("error_since")) or now
        if job is not None and not job.payload.get("error_since"):
            await repo.merge_payload(job_id, {"error_since": _iso(now)})
        wait = swarm.config.remote_fallback_minutes
        if wait is not None and now - since >= timedelta(minutes=wait):
            if job is not None:
                await self._stop_connected(ca, job, abandon=True)
                await self.messages.mark_unread(job.payload.get("messages") or [])
            await self.events.add(
                swarm.id, "info", team_id=team.id, task_id=task.id, actor=agent.id,
                detail=f"DAASH could not work with {ca.name} for {wait} minutes "
                       f"({connectors.ERROR_TEXT.get(exc.kind, exc.kind)}: {exc}), so "
                       f"{agent.name} ran '{task.name}' on the built-in agent instead.")
            return False
        raise agent_error(f"{ca.name} ({PROTOCOL_LABELS.get(ca.protocol, ca.protocol)}): "
                          f"{connectors.ERROR_TEXT.get(exc.kind, exc.kind)} ({exc})")

    async def _stop_connected(self, ca: ConnectedAgent, job, abandon: bool = False) -> None:
        """Close the job (its MCP calls are refused from now on) and stop a Hermes run; A2A
        cannot cancel, so a late reply is simply ignored."""
        repo = self.repos.remote_jobs
        await (repo.abandon(job.id) if abandon else repo.cancel(job.id))
        ext = (job.payload or {}).get("external_id")
        if ext:
            try:
                await connectors.cancel_work(ca, ext, transport=self.connector_transport)
            except ConnectorError as exc:
                log.info("could not stop %s's run %s: %s", ca.name, ext, exc)

    async def _cancel_connected_unless_active(self, ca: ConnectedAgent, job_id: str,
                                              swarm_id: str) -> None:
        """Pause or verification: cancel the job. An engine shutdown or restart (the swarm still
        ACTIVE) leaves it, and the next engine re-attaches and keeps polling."""
        fresh = await self.swarms.get(swarm_id)
        if fresh is None or fresh.lifecycle_state != SwarmStatus.ACTIVE.value:
            job = await self.repos.remote_jobs.get(job_id)
            if job is not None and job.status in ("queued", "leased"):
                await self._stop_connected(ca, job)

    async def _finish_connected(self, swarm: Swarm, team: Team, agent: Agent, task: Task,
                                ca: ConnectedAgent, job_id: str, progress) -> bool:
        """The agent finished: close the job (late MCP calls are refused), apply its reply's
        file blocks with the native path rules, then Review (or a failed attempt)."""
        repo = self.repos.remote_jobs
        blocks = remote.parse_file_blocks(progress.text)
        outcome = "submitted" if progress.state == "completed" else "failed"
        job = await repo.get(job_id)
        started = _parse_iso((job.payload if job else {}).get("sent_at")) or (
            job.created_at if job else _now())
        result = {"outcome": outcome, "summary": blocks.summary,
                  "files": [p for p, _ in blocks.files], "deleted": list(blocks.deleted),
                  "problems": blocks.problems,
                  "duration_ms": int((_now() - started).total_seconds() * 1000)}
        finished = await repo.finish_connected(job_id, result)
        if finished is None:            # cancelled while the reply came in: ignore it
            await self._require_state(swarm)
            return True
        await services.record_call(
            self.pool, agent, "work_task", CONNECTED_CALL_MODEL, usage=Usage(),
            task_id=task.id, ok=True, duration_ms=result["duration_ms"],
            transcript=[{"role": "connected", "connected_agent": ca.name,
                         "protocol": ca.protocol, "outcome": outcome,
                         "summary": blocks.summary,
                         "content": remote_tail(progress.text)}])
        await self.repos.connected.record_check(ca.id, True)
        await self._require_state(swarm)
        if await self._still(task.id, TaskStatus.IN_PROGRESS.value) is None:
            log.info("connected work on %r dropped: the task changed", task.name)
            return True
        who = f"{agent.name} ({via(ca)})"
        mcp_files = list(finished.payload.get("mcp_files") or [])
        if outcome != "submitted":
            await self._attempt_failed(
                swarm, team, agent, task, "", types.SimpleNamespace(files_written=mcp_files),
                f"{who} did not finish: {_short(progress.text) or 'it reported a failure'}.",
                blocks.summary, remote=True)
            return True
        ws = self.workspace(swarm)
        written, gone = await asyncio.to_thread(remote.apply_file_blocks, ws, blocks.files,
                                                blocks.deleted)
        files = list(dict.fromkeys([f for f in mcp_files if f not in gone] + written))
        summary = blocks.summary
        if blocks.problems:
            await self.events.add(
                swarm.id, "info", team_id=team.id, task_id=task.id, actor=agent.id,
                detail=f"DAASH could not use part of {ca.name}'s reply: "
                       + "; ".join(blocks.problems[:5]))
        await self.tasks.update(
            task.id, status=TaskStatus.REVIEW.value,
            deliverable={"summary": summary, "files": files, "harness": CONNECTED_HARNESS,
                         "connected_agent": ca.name, "connected_agent_id": ca.id,
                         "protocol": ca.protocol,
                         "mcp_calls": int(finished.payload.get("mcp_calls") or 0)})
        await self.events.add(
            swarm.id, "submitted", team_id=team.id, task_id=task.id, actor=agent.id,
            from_status=TaskStatus.IN_PROGRESS.value, to_status=TaskStatus.REVIEW.value,
            detail=f"{who} submitted '{task.name}' for review ({len(files)} file(s) written): "
                   f"{_short(summary, 200)}")
        await self.signal("task.submitted", team.id, task.id, files=files)
        return True


def agent_error(message: str):
    from app.jobs import RemoteError
    return RemoteError(message)


def remote_tail(text: str, limit: int = remote.TRANSCRIPT_MAX) -> str:
    return (text or "")[-limit:]


def _short(text: str, n: int = 300) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _listing(root) -> str:
    from app.jobs import workspace_listing
    return workspace_listing(root)


def _excerpts(root, task: Task) -> dict[str, str]:
    """Current content of the task's own files (artifact and check paths), so an agent without
    DAASH's file tools can edit rather than rewrite them blindly."""
    from app.jobs import read_deliverable_files, review_paths
    paths = review_paths(task.model_copy(update={"deliverable": {}}))
    return read_deliverable_files(root, paths[:EXCERPT_FILES], limit=EXCERPT_CHARS)
