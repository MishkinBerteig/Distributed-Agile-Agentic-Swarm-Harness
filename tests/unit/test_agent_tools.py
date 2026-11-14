"""DAASH's shared agent tools and team messages (docs/DESIGN-0.4.md sections 2.1 and 3),
against in-memory repositories (tests/unit/agent_fakes.py)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app import agent_tools as A
from app import prompts as P
from app.tools import ToolError, ToolRegistry
from tests.unit.agent_fakes import World, make_agent


def registry(ctx, workspace: bool = False) -> ToolRegistry:
    return ToolRegistry(A.build_agent_tools(ctx, workspace=workspace))


async def call(reg: ToolRegistry, tool: str, /, **args):
    return await reg.call(tool, args)


# -- the tool set -----------------------------------------------------------------------------------

def test_shared_tools_and_order():
    names = [t.name for t in A.build_agent_tools(None, workspace=False)]
    assert tuple(names) == A.SHARED_TOOL_NAMES
    with_ws = [t.name for t in A.build_agent_tools(None, workspace=True)]
    assert tuple(with_ws) == A.WORKSPACE_TOOL_NAMES + A.SHARED_TOOL_NAMES
    assert "submit" not in names and "submit" not in with_ws


def test_specs_for_the_builtin_agent_end_with_submit():
    specs = A.tool_specs(workspace=True, native=True)
    names = [s["function"]["name"] for s in specs]
    assert names[-1] == "submit" and names[:5] == list(A.WORKSPACE_TOOL_NAMES)
    by = {s["function"]["name"]: s["function"] for s in specs}
    assert by["send_message"]["parameters"]["required"] == ["text"]
    assert by["vote_on_proposal"]["parameters"]["required"] == ["proposal_id", "approve"]
    # the 0.3 descriptions are unchanged (the native agent's behaviour must not drift)
    assert by["remember"]["description"] == "Save a short note to your persistent memory."
    assert by["create_subtask"]["parameters"]["required"] == ["name"]


async def test_spec_only_tools_refuse_to_run():
    reg = registry(None)
    out = await call(reg, "remember", content="x")
    assert out.error and "no job attached" in out.output


# -- memory, subtasks, proposals (as in 0.3) ------------------------------------------------------------

async def test_remember_and_recall():
    w = World()
    reg = registry(w.ctx())
    assert (await call(reg, "recall")).output == "(no memories yet)"
    assert (await call(reg, "remember", content="Use a sieve")).output == "remembered"
    (mem,) = w.repos.memories.rows
    assert (mem.agent_id, mem.swarm_id, mem.team_id, mem.task_id) == ("a-d1", "s1", "tm1", "t1")
    assert (await call(reg, "recall", query="sieve")).output == "- Use a sieve"
    assert (await call(reg, "recall")).output == "- Use a sieve"     # query is optional
    assert (await call(reg, "remember", content="  ")).error
    assert (await call(reg, "recall", query=3)).error


async def test_optional_arguments_may_be_left_out():
    w = World()
    reg = registry(w.ctx())
    out = await call(reg, "create_subtask", name="Polish", acceptance_criteria=["Reads well"])
    assert not out.error, out.output
    out = await call(reg, "propose_team", name="Bench", mission_statement="Measure speed.")
    assert not out.error and "no reason given" in w.repos.events.of("proposal")[0].detail


async def test_create_subtask():
    w = World()
    reg = registry(w.ctx())
    out = await call(reg, "create_subtask", name="Tests", artifact="test_primes.py",
                     required_capabilities=["run_code", "bogus"])
    assert not out.error and "Pending" in out.output
    sub = next(t for t in w.repos.tasks.by_id.values() if t.name == "Tests")
    assert sub.parent_id == "t1" and sub.required_capabilities == ["run_code"]
    assert "created subtask 'Tests'" in w.repos.events.of("created")[0].detail
    assert (await call(reg, "create_subtask", name="No criteria")).error
    assert (await call(reg, "create_subtask", name="Bad", artifact="../x")).error


async def test_propose_list_and_vote():
    w = World()
    reg1, reg2 = registry(w.ctx(w.d1)), registry(w.ctx(w.d2))
    out = await call(reg1, "propose_team", name="Bench", mission_statement="Measure speed.")
    assert not out.error and "pending" in out.output
    assert (await call(reg1, "list_proposals")).output == \
        "(no pending proposals awaiting your vote)"      # the proposer voted already
    listing = (await call(reg2, "list_proposals")).output
    assert "new team 'Bench'" in listing and "1 for, 0 against" in listing
    pid = next(iter(w.repos.proposals.by_id))
    assert pid in listing
    assert "status approved" in (await call(reg2, "vote_on_proposal", proposal_id=pid,
                                            approve=True)).output
    assert (await call(reg2, "vote_on_proposal", proposal_id=pid, approve="yes")).error


# -- roster and current task ----------------------------------------------------------------------------

async def test_team_roster():
    w = World()
    text = (await call(registry(w.ctx()), "team_roster")).output
    lines = text.splitlines()
    assert lines[0].startswith("Team 'Primes'.")
    assert lines[1].startswith("- Mission Judgement: Mission Judgement")
    assert ("- Delivery-1 (you): Mission Delivery, agent type DAASH built-in agent, can "
            "edit_files; busy on 'Implement primes'") in text
    assert "- Delivery-2: Mission Delivery, agent type Codex, can edit_files, run_code; idle" in text
    assert "Swarm Coordinator" not in "\n".join(lines[1:-1])    # team members only
    assert '"team"' in lines[-1] and '"coordinator"' in lines[-1]


async def test_current_task_shows_feedback_and_job():
    w = World()
    w.repos.tasks.by_id["t1"] = w.task.model_copy(update={"attempts": 1,
                                                          "feedback": "Handle n < 2."})
    text = (await call(registry(w.ctx(job_id="job-9")), "current_task")).output
    assert text.startswith("Task t1: Implement primes")
    assert "1. primes.py exists [file_exists path=primes.py]" in text
    assert "Status: In-Progress (attempt 2)" in text
    assert "Latest feedback from the judges (fix every point):\nHandle n < 2." in text
    assert text.endswith("DAASH job: job-9")


# -- messages -------------------------------------------------------------------------------------------

def test_recipients_by_name_team_and_coordinator():
    w = World()
    agents = [w.d1, w.d2, w.d3, w.mj, w.qj, w.coord, w.other]
    assert A.message_recipients(w.d1, agents, "delivery-2") == ("direct", [w.d2])
    assert A.message_recipients(w.d1, agents, "  Delivery-3 ") == ("direct", [w.d3])
    aud, to = A.message_recipients(w.d1, agents, "TEAM")
    assert aud == "team" and to == [w.d2, w.d3]          # not the sender, judges or other teams
    assert A.message_recipients(w.d1, agents, "coordinator") == ("coordinator", [w.coord])
    assert A.message_recipients(w.d1, agents, "Jane", {"jane": "a-d2"}) == ("direct", [w.d2])
    with pytest.raises(ToolError, match="yourself"):
        A.message_recipients(w.d1, agents, "Delivery-1")
    for bad in ("Mission Judgement", "nobody", ""):
        with pytest.raises(ToolError, match="send to one of: Delivery-2, Delivery-3, team, "
                                             "coordinator"):
            A.message_recipients(w.d1, agents, bad)
    alone = make_agent("x", "Delivery-1", team_id="solo")
    with pytest.raises(ToolError, match="only delivery agent"):
        A.message_recipients(alone, [alone, w.coord], "team")


async def test_send_message_fans_out_and_records_an_event():
    w = World()
    reg = registry(w.ctx())
    out = await call(reg, "send_message", to="team", text="I own primes.py; please do not edit it.")
    assert out.output == "message sent to 2 team member(s): Delivery-2, Delivery-3"
    rows = w.repos.messages.rows
    assert [r.to_agent_id for r in rows] == ["a-d2", "a-d3"]
    assert len({r.send_id for r in rows}) == 1 and {r.audience for r in rows} == {"team"}
    assert {(r.task_id, r.attempt, r.team_id) for r in rows} == {("t1", 0, "tm1")}
    (ev,) = w.repos.events.of("message")
    assert ev.detail == "Delivery-1 → team: I own primes.py; please do not edit it."
    assert ev.actor == "a-d1" and ev.task_id == "t1"
    assert (await call(reg, "send_message", to="Delivery-2", text="hi")).output == \
        "message sent to Delivery-2"
    assert w.repos.events.of("message")[-1].detail == "Delivery-1 → Delivery-2: hi"
    assert (await call(reg, "send_message", to="coordinator", text="We need a docs team.")).output \
        == "message sent to the Swarm Coordinator"
    assert w.repos.events.of("message")[-1].detail.startswith("Delivery-1 → coordinator: ")


async def test_send_message_accepts_message_for_text_and_defaults_to_the_team():
    # Live run 2026-11-13: OpenClaw's first two calls used `message=` and then left out `to`.
    w = World()
    reg = registry(w.ctx())
    out = await call(reg, "send_message", to="team", message="tests are done")
    assert not out.error and out.output == "message sent to 2 team member(s): Delivery-2, Delivery-3"
    out = await call(reg, "send_message", text="README is done")
    assert not out.error and out.output.startswith("message sent to 2 team member(s)")
    assert [r.text for r in w.repos.messages.rows][::2] == ["tests are done", "README is done"]
    assert (await call(reg, "send_message", to="team")).error            # still needs some text


async def test_send_message_event_is_clipped_and_long_messages_refused():
    w = World()
    reg = registry(w.ctx())
    text = "word " * 100
    await call(reg, "send_message", to="Delivery-2", text=text)
    detail = w.repos.events.of("message")[0].detail
    assert len(detail.split(": ", 1)[1]) == A.MESSAGE_EVENT_CLIP and detail.endswith("…")
    out = await call(reg, "send_message", to="Delivery-2", text="x" * (A.MESSAGE_MAX_CHARS + 1))
    assert out.error and "limit is 4000" in out.output
    assert (await call(reg, "send_message", to="Delivery-2", text="  ")).error


async def test_twenty_sends_per_attempt():
    w = World()
    reg = registry(w.ctx())
    for i in range(A.MESSAGES_PER_ATTEMPT):
        assert not (await call(reg, "send_message", to="team", text=f"m{i}")).error
    assert len(w.repos.messages.rows) == 2 * A.MESSAGES_PER_ATTEMPT   # a team send counts once
    out = await call(reg, "send_message", to="Delivery-2", text="one more")
    assert out.error and "20 messages in this attempt" in out.output
    # the next attempt has a fresh allowance
    w.task = w.task.model_copy(update={"attempts": 1})
    assert not (await call(registry(w.ctx()), "send_message", to="Delivery-2", text="new")).error


async def test_read_messages_oldest_first_then_marked_read():
    w = World()
    await call(registry(w.ctx(w.d2)), "send_message", to="Delivery-1", text="first")
    await call(registry(w.ctx(w.d3)), "send_message", to="team", text="second")
    reg = registry(w.ctx())
    text = (await call(reg, "read_messages")).output
    lines = text.splitlines()
    assert lines[0].startswith("- From Delivery-2 (to you, 2026-11-11") and \
        lines[0].endswith("): first")
    assert lines[1].startswith("- From Delivery-3 (to the team, ") and lines[1].endswith("second")
    assert (await call(reg, "read_messages")).output == "(no unread messages)"
    # Delivery-2 still has its copy of the team message
    assert "second" in (await call(registry(w.ctx(w.d2)), "read_messages")).output


# -- prompts --------------------------------------------------------------------------------------------

def msg(name, text, audience="direct", team=None):
    return SimpleNamespace(from_name=name, text=text, audience=audience, from_team=team)


def test_messages_block_in_every_delivery_work_prompt():
    t = World().task
    assert P.team_messages_block(None) == ""
    empty = P.team_messages_block([])
    assert "Messages from your team: none." in empty and P.MESSAGING_LINE in empty
    block = P.team_messages_block([msg("Delivery-2", "keep the TypeError:\n\n    raise TypeError"),
                                   msg("Delivery-3", "README is mine", "team")])
    assert "Messages from your team (now marked read):\n- Delivery-2 to you: keep the TypeError:" \
           "\n      raise TypeError\n" \
           "- Delivery-3 to the team: README is mine\n" + P.MESSAGING_LINE in block
    long = "x" * 4000                                      # shown whole: never cut in a prompt
    assert long in P.team_messages_block([msg("Delivery-2", long)])
    native = P.work_prompt(t, t.criteria, None, "", (), messages=[msg("Delivery-2", "hello")])
    remote = P.remote_work_prompt(t, t.criteria, None, "", messages=[msg("Delivery-2", "hello")])
    for p in (native, remote):
        assert "- Delivery-2 to you: hello" in p and P.MESSAGING_LINE in p
        # the section sits after the workspace listing, before the final instructions
        assert p.index("Existing workspace files") < p.index("Messages from your team") \
            < p.index("Before finishing")
    assert "send_message" not in P.work_prompt(t, t.criteria, None, "")   # pre-0.4 callers


def test_remote_prompt_names_the_daash_tools():
    t = World().task
    p = P.remote_work_prompt(t, t.criteria, None, "")
    assert P.DAASH_TOOLS_REMOTE in p
    assert "You have DAASH tools (MCP server `daash`): remember/recall for your persistent " \
           "memory, team_roster, send_message/read_messages to coordinate with your team, " \
           "create_subtask, propose_team, vote_on_proposal. Use them." in p
    assert "submit" not in p
    assert "DAASH tools" not in P.work_prompt(t, t.criteria, None, "")


def test_coordinator_prompts_include_messages_to_the_coordinator():
    notes = [msg("Delivery-1", "Docs describe a missing function", team="Primes")]
    block = P.coordinator_messages_block(notes)
    assert block.startswith("Messages the teams sent to you, the Swarm Coordinator")
    assert "- Delivery-1 (team 'Primes'): Docs describe a missing function" in block
    assert P.coordinator_messages_block([]) == ""
    vision = P.vision_review_prompt("V", "dod", [], {}, "", {}, messages=notes)
    assert block in vision and vision.startswith("Job: review the whole swarm")
    assert vision.index(block) < vision.index("Reply with ONLY one JSON object")
    boot = P.bootstrap_prompt(3, True, messages=notes)
    assert boot.startswith("Job: bootstrap the swarm") and block in boot
    assert "Messages the teams" not in P.vision_review_prompt("V", "dod", [], {}, "", {})


# -- the file tools of connected agents ------------------------------------------------------------------

async def test_workspace_tools_edit_the_live_workspace_and_report_files(tmp_path: Path):
    w = World()
    seen: list[tuple[str, bool]] = []

    async def on_file(path: str, written: bool) -> None:
        seen.append((path, written))

    (tmp_path / ".daash").mkdir()
    (tmp_path / ".daash" / "secret").write_text("x")
    reg = registry(w.ctx(workspace=tmp_path, on_file=on_file), workspace=True)
    assert not (await call(reg, "write_file", path="docs/a.md", content="hello")).error
    assert (tmp_path / "docs" / "a.md").read_text() == "hello"
    assert not (await call(reg, "append_file", path="docs/a.md", content="!")).error
    assert (await call(reg, "read_file", path="docs/a.md")).output == "hello!"
    listing = (await call(reg, "list_files")).output
    assert "docs/a.md" in listing and ".daash" not in listing
    assert not (await call(reg, "delete_file", path="docs/a.md")).error
    assert seen == [("docs/a.md", True), ("docs/a.md", True), ("docs/a.md", False)]
    for name, args in (("read_file", {"path": ".daash/secret"}),
                       ("write_file", {"path": ".daash/x", "content": "y"}),
                       ("write_file", {"path": "../escape", "content": "y"})):
        out = await reg.call(name, args)
        assert out.error, (name, args)
    assert (await reg.call("read_file", {"path": ".daash/secret"})).output == \
        "Error: .daash/ is reserved for DAASH"
