"""DAASHboard online help: "DAASH Agent Prompts" (GET /help/prompts).

The page explains how every agent prompt is built. Its examples are rendered live from
app/prompts.py with fixed sample data, and its limits are read from the code's own constants,
so the help cannot drift from the prompts DAASH really sends. Pure: no I/O.
"""
from __future__ import annotations

import html
import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app import jobs as J
from app import models
from app import prompts as P
from app.engine import DEFER_SECONDS, ESCALATE_AFTER
from app.config import Settings
from app.llm import NUDGE, TRANSCRIPT_CHARS, LLMClient
from app.tools import READ_LIMIT_CHARS, ToolRegistry, workspace_tools

TITLE = "DAASH Agent Prompts"
_TEMPERATURE = inspect.signature(LLMClient.chat).parameters["temperature"].default
_MAX_TOKENS = Settings.model_fields["LLM_MAX_TOKENS"].default
_TASK_LIST_LIMIT = inspect.signature(P._task_list).parameters["limit"].default

# --------------------------------------------------------------------------- sample swarm

_NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
VISION = ("A developer can copy a tiny, dependable prime-number module into any project: "
          "primes.py (standard library only) with is_prime(n) and primes_up_to(n), a unittest "
          "suite that passes with `python3 -m unittest`, and a short README.md.")
VALUES = ("Be truthful: never invent facts.\n"
          "Keep artifacts small, clear and useful to a beginner.")
VISION_DOD = ("- primes.py exposes is_prime(n) and primes_up_to(n)\n"
              "- `python3 -m unittest` passes from the workspace root\n"
              "- README.md has one usage example per function")
MISSION = ("To advance the Vision, build primes.py and its tests, and document both in "
           "README.md.")
TEAM_DOD = ("- The task's file exists in the workspace root\n"
            "- Code uses only the Python standard library\n"
            "- Changes are confined to the task's own file")
TEAM_DOR = "- The task names the one file it creates or changes"
FEEDBACK = ["Keep the README under 40 lines."]
LEARNINGS = [
    "Put the exact run command and its success condition into the task text so acceptance "
    "is checkable by script.",
    "Give each deliverable file its own task (code, tests, docs) so one rejection stays "
    "isolated."]
MEMORIES = ["The team chose a Sieve of Eratosthenes for primes_up_to."]
TEAM = {"name": "Primes", "status": "active", "mission_statement": MISSION}
OTHER_TEAM = {"name": "Docs site", "status": "active",
              "mission_statement": "To advance the Vision, publish a one-page HTML reference "
                                   "for the module."}
PRIMES_PY = '''"""Prime number helpers (standard library only)."""


def is_prime(n):
    """Return True if n is prime. False for n < 2; TypeError for non-integers."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError("n must be an int")
    if n < 2:
        return False
    i = 2
    while i * i <= n:
        if n % i == 0:
            return False
        i += 1
    return True
'''
LISTING = "README.md (612 bytes)\nprimes.py (402 bytes)\ntest_primes.py (1180 bytes)"


def _crit(ordinal, text, kind="rubric", path=None, arg=None, status="unknown", evidence=None):
    return models.AcceptanceCriterion(id=f"c{ordinal}", task_id="t", ordinal=ordinal, text=text,
                                      check_kind=kind, check_path=path, check_arg=arg,
                                      status=status, evidence=evidence)


def _task(tid, name, description, artifact, criteria, status="Pending", needs=()):
    return models.Task(id=tid, swarm_id="s", team_id="tm", name=name, description=description,
                       artifact=artifact, status=status, criteria=criteria,
                       required_capabilities=list(needs), created_at=_NOW, updated_at=_NOW)


SMOKE = 'python3 -c "import primes; assert primes.is_prime(97); assert not primes.is_prime(1)"'
TASK = _task(
    "7c1e9a52-4d0b-4f3e-9a61-2b8f0c6d1e47", "Implement primes module",
    "Create primes.py with is_prime(n) (False for n < 2, TypeError for non-integers) and "
    "primes_up_to(n).", "primes.py",
    [_crit(1, "primes.py exists", "file_exists", "primes.py"),
     _crit(2, "primes.py defines is_prime(n)", "file_contains", "primes.py", r"def is_prime\("),
     _crit(3, "The smoke test passes", "command", None, SMOKE),
     _crit(4, "Docstrings explain the edge cases")], needs=["run_code"])
REVIEWED = TASK.model_copy(update={
    "status": "Review",
    "deliverable": {"summary": "Wrote primes.py with both functions and edge-case docstrings; "
                               "the smoke test passes.", "files": ["primes.py"]},
    "criteria": [
        _crit(1, "primes.py exists", "file_exists", "primes.py", status="green",
              evidence="File 'primes.py' exists (402 bytes)."),
        _crit(2, "primes.py defines is_prime(n)", "file_contains", "primes.py",
              r"def is_prime\(", status="green", evidence="def is_prime("),
        _crit(3, "The smoke test passes", "command", None, SMOKE, status="green",
              evidence="exit 0."),
        _crit(4, "Docstrings explain the edge cases")]})
TESTS_TASK = _task(
    "2f4b8d10-6c3a-4e9b-8d72-5a1c9e3f0b68", "Write unittest suite",
    "Create test_primes.py covering both functions, including n < 2 and non-integers.",
    "test_primes.py",
    [_crit(1, "test_primes.py exists", "file_exists", "test_primes.py"),
     _crit(2, "The suite passes", "command", None, "python3 -m unittest")], needs=["run_code"])
README_TASK = _task(
    "9a0d3e7c-1b5f-4a28-b6e4-7f2c8d9a1053", "Write README",
    "Write README.md with one usage example per function.", "README.md",
    [_crit(1, "README.md exists", "file_exists", "README.md"),
     _crit(2, "Each function has exactly one usage example")])
DONE = [t.model_copy(update={"status": "Done"}) for t in (TASK, TESTS_TASK, README_TASK)]
NATIVE = models.harness_capabilities("daash")
CLI = models.harness_capabilities("claude-code")


def _system(role: str, with_team: bool = True) -> str:
    return P.compose_system_prompt(
        vision=VISION, role=role, values_principles=VALUES,
        mission=MISSION if with_team else None, team_name="Primes" if with_team else None,
        vision_dod=VISION_DOD, team_dor=TEAM_DOR if with_team else None,
        team_dod=TEAM_DOD if with_team else None, feedback=FEEDBACK, learnings=LEARNINGS,
        memories=MEMORIES if role == "mission_delivery" else ())


def _rework() -> str:
    fb = J.combine_feedback(
        [], TASK,
        P.QualityReview(passed=False, quality_score=0.8, feedback="Handle n < 2.",
                        criteria={4: (False, 0.8, "The is_prime docstring does not say what "
                                                  "happens for negative numbers.")}),
        P.MissionReview(expected_value_met=True, confidence=0.95, reasoning="",
                        feedback="primes_up_to is missing; the Mission needs both functions."),
        ["primes.py defines is_prime(n) (no match for 'def is_prime\\(')"])
    full = P.work_prompt(TASK, TASK.criteria, fb, LISTING, (), capabilities=NATIVE)
    return full[full.find("The previous attempt"):full.find("\nExisting workspace files")]


def _vision() -> str:
    teams = [dict(TEAM, status="accomplished"), dict(OTHER_TEAM, status="accomplished")]
    return P.vision_review_prompt(
        VISION, VISION_DOD, teams, {"Primes": DONE, "Docs site": []}, LISTING + "\nindex.html "
        "(2048 bytes)", {"primes.py": PRIMES_PY[:4000]})


def _retro() -> str:
    swarm = SimpleNamespace(vision_statement=VISION, definition_of_done=VISION_DOD)
    team = SimpleNamespace(id="tm", name="Primes", status="accomplished", generation=1,
                           mission_statement=MISSION)
    tasks = [t.model_copy(update={"complexity": "simple", "attempts": 1,
                                  "deliverable": {"model": "fast"}}) for t in DONE]
    return P.retrospective_prompt(J.retrospective_summary(swarm, [team], tasks, {}, 312.0,
                                                          418_000))


def _assess() -> str:
    return (J.assess_context(LISTING, {"primes.py": PRIMES_PY})
            + P.assess_prompt(TEAM, DONE, [], [OTHER_TEAM]))


def _no_wait_sentence() -> str:
    p = P.prioritize_prompt([TASK], [], None, (), slots=1, can_wait=False)
    return p[p.find(" No other work"):p.find("3. rejections")].strip()


def _tools() -> list[dict]:
    reg = ToolRegistry()
    for t in workspace_tools(Path("."), reg):
        reg.add(t)
    # The engine tools' handlers are closures over a live job; only their specs are read here.
    for t in J.Jobs._engine_tools(None, None, None, None, None, {}):  # type: ignore[arg-type]
        reg.add(t)
    return reg.specs()


# --------------------------------------------------------------------------- job catalogue

def _cfg() -> models.SwarmConfig:
    return models.SwarmConfig()


def _model(role: str) -> str:
    c = _cfg()
    return {"coordinator": c.coordinator.model, "qj": c.role_models.quality_judgement,
            "mj": c.role_models.mission_judgement, "delivery": c.role_models.mission_delivery,
            "classifier": c.role_models.classifier}[role]


ROLES = [
    ("coordinator", "Swarm Coordinator", "swarm_coordinator",
     "One per swarm. Turns the Vision into a vision-level Definition of Done and teams, checks "
     "the combined result, and runs the retrospective."),
    ("qj", "Quality Judgement", "quality_judgement",
     "One per team. Sets the team's standards and scores every deliverable against its "
     "acceptance criteria."),
    ("mj", "Mission Judgement", "mission_judgement",
     "One per team. Approves what to work on next, judges whether each deliverable advances "
     "the Mission, and decides when the Mission is accomplished."),
    ("delivery", "Mission Delivery", "mission_delivery",
     "Several per team. Plans tasks, does the work (on the built-in harness or an external "
     "agent CLI) and votes on proposals for new teams."),
]


def _jobs() -> list[dict]:
    lim, thr = models.Limits(), models.DoDThresholds()
    return [
        dict(role="coordinator", id="bootstrap", name="Bootstrap the swarm",
             when="A new swarm starts.",
             adds=["The largest number of teams allowed, and whether teams must be defined "
                   "(they are not when you created teams yourself)."],
             reply="A vision-level Definition of Done of 3 to 6 measurable statements and, when "
                   "needed, the teams, each with a name and a Mission that refers to the Vision.",
             checked="Every Mission must be a real sentence; duplicate team names are dropped; "
                     "the team count is capped.",
             example=lambda: P.bootstrap_prompt(lim.max_teams, True)),
        dict(role="coordinator", id="review-vision", name="Review the vision",
             when=f"Every team has finished its Mission, before the swarm goes to Verification "
                  f"(at most {models.MAX_VISION_REVIEWS} times per swarm).",
             adds=["The Vision (up to 2,000 characters) and the vision-level Definition of Done "
                   "(up to 3,000).",
                   "Each team's Mission (up to 500 characters) and Done tasks (up to 20).",
                   "The workspace file listing and up to 12 deliverable files, up to 4,000 "
                   "characters each."],
             reply="A verdict (met or gaps), a confidence, a summary, and for each gap the exact "
                   "team that must act and what it must change or add.",
             checked=f"Team names must match exactly. Anything short of \"met\" at "
                     f"{thr.mission_confidence} or more must name a gap, or the answer is sent "
                     "back. Each gap reopens that team with the gap as its focus.",
             example=_vision),
        dict(role="coordinator", id="retrospective", name="Retrospective",
             when="The swarm reaches Learning.",
             adds=["The swarm's history, up to 12,000 characters: Vision, Definition of Done, "
                   "tokens used, p90 cycle time, and per team the task counts and each Done or "
                   "Failed task's attempts, complexity, model and last judge feedback."],
             reply="1 to 8 learnings, each a pattern, an automation or a routing lesson.",
             checked="Stored as global learnings, which every future swarm can draw on.",
             example=_retro),
        dict(role="qj", id="standards", name="Set the team's standards",
             when="A team is created, before it plans any work. You can regenerate the "
                  "standards with your own guidance.",
             adds=["Your guidance for the Definition of Ready and Definition of Done, if any."],
             reply="Team-specific Definition of Ready rules and the team's Definition of Done.",
             checked="The Definition of Done must not be empty. The prompt rules out readiness "
                     "rules that cannot be checked before work starts, that require a file not "
                     "to exist, or that ask one task to cover the whole Mission.",
             example=lambda: P.standards_prompt()),
        dict(role="qj", id="review-quality", name="Review quality",
             when="A task is submitted for review. It runs at the same time as Mission "
                  "Judgement's review.",
             adds=["What the delivering agent could not do (for example run code), so claims "
                   "of having done it are not accepted.",
                   "Every criterion: deterministic checks arrive already decided, with their "
                   "evidence; command results come from DAASH's runner and are marked as "
                   "trusted.",
                   f"The deliverable files, up to {J.FILE_CLIP:,} characters each."],
             reply="passed, an overall quality score, and for each judged (rubric) criterion a "
                   "score and a reason, plus feedback when it does not pass.",
             checked=f"Every rubric criterion must be scored. A score below 1.0 must name a "
                     f"concrete defect. The task passes only with every deterministic check "
                     f"green and every rubric score above {thr.rubric}.",
             example=lambda: P.quality_review_prompt(REVIEWED, REVIEWED.criteria,
                                                     {"primes.py": PRIMES_PY}, CLI)),
        dict(role="mj", id="prioritize", name="Prioritize",
             when="The team has Pending tasks whose dependencies are done.",
             adds=["The other teams' Missions, so the team stays in its own scope.",
                   "How many tasks may start now (free delivery slots).",
                   "While other work is in flight anywhere in the swarm, permission to wait "
                   f"(nothing starts; it asks again when a task is Done, or after "
                   f"{DEFER_SECONDS // 60} minutes). Otherwise: approve or reject, because "
                   "waiting cannot change anything.",
                   "The team's Definition of Ready, the candidate tasks with their criteria, "
                   "the Done tasks, and the swarm-wide file listing."],
             reply="The priority order, the tasks approved to start, rejections with reasons, "
                   "and a reason when it waits.",
             checked="Task ids must match (a mistype of up to 2 characters is accepted). "
                     "Forgotten ids go to the end of the order. A rejected task is gone for "
                     "good; one that cannot start yet stays Pending.",
             example=lambda: P.prioritize_prompt([TASK, TESTS_TASK, README_TASK], [], TEAM_DOR,
                                                 [OTHER_TEAM], slots=2, workspace_files=""),
             example2=(_no_wait_sentence, "The sentence that replaces the waiting rule when "
                                          "no other work is in flight")),
        dict(role="mj", id="review-mission", name="Review the mission value",
             when="A task is submitted for review.",
             adds=["What the delivering agent could not do.",
                   "The task with its criteria and the agent's own summary (up to 2,000 "
                   "characters).",
                   f"The deliverable files, up to {J.FILE_CLIP:,} characters each."],
             reply="Whether the expected value is met, a confidence, reasoning and feedback.",
             checked=f"An answer that would send the work back must say what to fix, or it is "
                     f"sent back. The task needs the value met with confidence "
                     f"{thr.mission_confidence} or more.",
             example=lambda: P.mission_review_prompt(
                 REVIEWED, REVIEWED.deliverable["summary"], {"primes.py": PRIMES_PY}, CLI)),
        dict(role="mj", id="assess", name="Assess the mission",
             when="The team has no open tasks.",
             adds=[f"Every file in the workspace (up to {J.LISTING_LIMIT}).",
                   f"Up to {J.ASSESS_MAX_FILES} deliverable excerpts of 1,500 to 4,000 "
                   f"characters, {J.ASSESS_TOTAL_CHARS:,} in all.",
                   "The other teams' Missions, and the team's Done and Failed tasks."],
             reply="continue, accomplished or restart, with a confidence, reasoning and the "
                   "next focus.",
             checked=f"Anything short of accomplished at {thr.mission_confidence} must name the "
                     "missing content as the next focus, or it is sent back. The next plan "
                     "starts from that focus.",
             example=_assess),
        dict(role="delivery", id="plan", name="Plan tasks",
             when="The team has no open tasks and task budget left. The team's first delivery "
                  "agent plans.",
             adds=["The other teams' Missions.",
                   "What each delivery agent on the team can do.",
                   "Mission Judgement's next focus, if any.",
                   "Done and open tasks (up to 30; descriptions up to 200 characters) and the "
                   "workspace file listing.",
                   "The kinds of checks a criterion can use. When an agent can run code, the "
                   "rule that \"tests pass\" must be a command check."],
             reply=f"1 to {lim.max_new_tasks_per_plan} tasks, each with one artifact, "
                   "acceptance criteria, dependencies and the capabilities it needs.",
             checked="A check that cannot run (a bad pattern, path or command) is sent back once "
                     "with the reason. If it is still wrong, it becomes a judged criterion and "
                     "the event log says so. A file-exists check is added for the artifact when "
                     "no file check exists. A command check means the task needs run_code.",
             example=lambda: P.plan_prompt("Primes", [], [], lim.max_new_tasks_per_plan, None,
                                           "", [OTHER_TEAM],
                                           agent_capabilities=[NATIVE, CLI])),
        dict(role="delivery", id="classify", name="Classify the task",
             when="Before a task's first attempt. Uses the delivery agent's system message and "
                  f"the classifier model ({_model('classifier')}).",
             adds=["The task with its criteria."],
             reply="simple, moderate or complex.",
             checked="If the call fails the task counts as moderate. Complex tasks start one "
                     "model step higher.",
             example=lambda: P.classify_prompt(TASK)),
        dict(role="delivery", id="work", name="Do the work (built-in agent)",
             when="A task assigned to a built-in agent is In Progress.",
             adds=["The scope rule: change only this task's files.",
                   "What the agent can do, and what the task needs that it lacks.",
                   "The task, its artifact and its criteria.",
                   "On a rework attempt, everything the judges said (see Rework feedback).",
                   "The workspace file listing and any team proposal awaiting its vote."],
             reply=f"Tool calls, ending with submit, within {lim.max_tool_steps} steps.",
             checked="If the agent answers in text without submitting, it is reminded once. "
                     "Not submitting counts as a failed attempt.",
             example=lambda: P.work_prompt(TASK, TASK.criteria, None, "", (),
                                           capabilities=NATIVE)),
        dict(role="delivery", id="work-cli", name="Do the work (external agent)",
             when="A task assigned to an OpenClaude, Claude Code or Codex agent is In Progress. "
                  "A runner picks up the job.",
             adds=["The same as the built-in agent, without DAASH's tools or proposals, plus "
                   "\"do not ask questions\". The agent works on a copy of the workspace with "
                   "its own tools."],
             reply="The CLI's final message is the summary. The runner then runs the task's "
                   "command checks and sends the files and results back.",
             checked="The changed files are validated and applied to the shared workspace.",
             example=lambda: P.remote_work_prompt(TASK, TASK.criteria, None, "",
                                                  capabilities=CLI)),
        dict(role="delivery", id="vote", name="Vote on a new team",
             when="A delivery agent proposes a new team. Every delivery agent of the team that "
                  "has not voted is asked; the majority decides.",
             adds=["The proposal (its reason up to 600 characters) and the team's open and Done "
                   "tasks."],
             reply="approve or not, with a one-sentence reason.",
             checked="approve must be true or false.",
             example=lambda: P.vote_prompt(
                 SimpleNamespace(name="Benchmarks", mission_statement="To advance the Vision, "
                                 "measure and document the speed of both functions.",
                                 reason="Performance is outside our Mission."),
                 "Primes", [README_TASK], DONE[:2])),
    ]


# --------------------------------------------------------------------------- HTML

def _e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _pre(text: str, label: str) -> str:
    return (f'<details class="example"><summary>{_e(label)} '
            f'<span class="size">{len(text):,} characters</span></summary>'
            f'<pre>{_e(text.rstrip())}</pre></details>')


def _table(head: list[str], rows: list[list[str]], cls: str = "") -> str:
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (f'<div class="scroll"><table class="{cls}"><thead><tr>{th}</tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


CSS = """
:root {
  /* DAASHboard palette; layout: sticky contents rail beside one reading column */
  --bg: #f4f5f8; --surface: #ffffff; --border: #dfe3ea; --text: #1c2330; --muted: #5f6b80;
  --accent: #3b5bdb; --accent-soft: #e7ecff; --code-bg: #f0f2f7;
  --r-coord: #3b5bdb; --r-qj: #7048b8; --r-mj: #1971c2; --r-del: #2b8a3e;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #10131a; --surface: #171b24; --border: #2a3142; --text: #e2e6ee; --muted: #9aa4b5;
  --accent: #8ea6ff; --accent-soft: #1d2540; --code-bg: #0c0f15;
  --r-coord: #8ea6ff; --r-qj: #b69cf0; --r-mj: #6cb4f2; --r-del: #6fcf86; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #10131a; --surface: #171b24; --border: #2a3142; --text: #e2e6ee; --muted: #9aa4b5;
  --accent: #8ea6ff; --accent-soft: #1d2540; --code-bg: #0c0f15;
  --r-coord: #8ea6ff; --r-qj: #b69cf0; --r-mj: #6cb4f2; --r-del: #6fcf86; color-scheme: dark }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 var(--font); }
.wrap { max-width: 1180px; margin: 0 auto; padding-block: 28px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px)); }
header.mast { display: grid; gap: 6px; padding-bottom: 22px; border-bottom: 1px solid var(--border); }
.eyebrow { font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: var(--accent); font-weight: 600; }
h1 { font-size: clamp(26px, 4vw, 34px); line-height: 1.15; margin: 0; text-wrap: balance; }
h2 { font-size: 22px; margin: 0 0 10px; text-wrap: balance; }
h3 { font-size: 17px; margin: 0; text-wrap: balance; }
p, li { max-width: 72ch; }
p { margin: 6px 0 10px; }
.lede { color: var(--muted); font-size: 16px; margin: 0; }
a { color: var(--accent); }
code { font-family: var(--mono); font-size: .88em; background: var(--code-bg); padding: 1px 4px; border-radius: 4px; }
.layout { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 36px; margin-top: 26px; }
nav.toc { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 16px); align-self: start;
  display: grid; gap: 2px; font-size: 14px; }
nav.toc a { color: var(--muted); text-decoration: none; padding: 3px 8px; border-radius: 6px; }
nav.toc a:hover, nav.toc a:focus-visible { color: var(--text); background: var(--accent-soft); outline: none; }
nav.toc .sub { padding-left: 20px; font-size: 13px; }
main { min-width: 0; display: grid; gap: 40px; }
section { display: grid; gap: 8px; min-width: 0; }
.stack { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.msg { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; min-width: 0; }
.msg h3 { font-size: 13px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }
.msg ol { margin: 0; padding-left: 22px; display: grid; gap: 2px; }
.scroll { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; vertical-align: top; padding: 8px 12px; border-bottom: 1px solid var(--border); }
th { font-size: 12px; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); font-weight: 600; background: var(--bg); }
tbody tr:last-child td { border-bottom: none; }
td.num, .num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.role { display: grid; gap: 14px; }
.role-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px; }
.chip { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 999px; border: 1px solid currentColor; }
.coordinator { color: var(--r-coord); } .qj { color: var(--r-qj); } .mj { color: var(--r-mj); } .delivery { color: var(--r-del); }
.job { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; display: grid; gap: 10px; min-width: 0; }
.job h3 { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; }
.job h3 code { font-size: 12px; font-weight: 400; color: var(--muted); }
dl { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 6px 14px; margin: 0; }
dt { font-size: 12px; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); padding-top: 2px; }
dd { margin: 0; min-width: 0; }
dd ul { margin: 0; padding-left: 18px; }
details.example { border: 1px solid var(--border); border-radius: 8px; background: var(--code-bg); min-width: 0; }
details.example summary { cursor: pointer; padding: 8px 12px; font-size: 14px; color: var(--accent); }
details.example summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.size { color: var(--muted); font-size: 12px; margin-left: 6px; }
pre { margin: 0; padding: 12px 14px; border-top: 1px solid var(--border); font: 12.5px/1.5 var(--mono);
  white-space: pre-wrap; overflow-wrap: anywhere; max-height: 520px; overflow: auto; }
.note { border-left: 3px solid var(--accent); background: var(--accent-soft); padding: 10px 14px; border-radius: 0 8px 8px 0; max-width: 72ch; }
@media (max-width: 860px) {
  .layout { grid-template-columns: minmax(0, 1fr); gap: 20px; }
  nav.toc { position: static; display: flex; flex-wrap: wrap; gap: 4px; }
  nav.toc .sub { display: none; }
  dl { grid-template-columns: minmax(0, 1fr); }
  dt { padding-top: 6px; }
}
@media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; } }
"""


def _system_section() -> str:
    rows = [
        ["1", "<code># Vision</code>", "The swarm's Vision, as you wrote it.", "none"],
        ["2", "<code># Values and Principles</code>", "Global settings (Values and Principles "
         "on the start screen).", "none"],
        ["3", "<code># Mission</code>", "The team's Mission. Left out for the Swarm "
         "Coordinator.", "none"],
        ["4", "<code># Definition of Done</code>", "Vision level (from the Swarm Coordinator) "
         "and team level (from Quality Judgement).", "none"],
        ["5", "<code># Definition of Ready</code>", "The team's own readiness rules. The generic "
         "rules are enforced by DAASH itself.", "none"],
        ["6", "<code># Your Role</code>", "A fixed description of the agent's role.", "fixed"],
        ["7", "<code># User Feedback</code>", "Feedback you sent from the swarm header. It is "
         "swarm-wide: every agent of the swarm receives it.", "none"],
        ["8", "<code># Learnings</code>", "Retrieved: see Memory and learnings.",
         f"{P._MAX_CONTEXT_ITEMS}"],
        ["9", "<code># Your Memory</code>", "Retrieved: this agent's own notes.",
         f"{P._MAX_CONTEXT_ITEMS}"],
    ]
    return (
        '<section id="system"><h2>The system message</h2>'
        "<p>Every call an agent makes starts with the same system message, built in a fixed "
        "order. Sections with nothing in them are left out. Only the retrieved learnings and "
        "memories change from one job to the next.</p>"
        + _table(["#", "Section", "Comes from", "Limit"], rows)
        + _pre(_system("mission_delivery"), "Example: a delivery agent's system message")
        + "</section>")


def _retrieval_section() -> str:
    queries = [
        ["Swarm Coordinator jobs", "The Vision"],
        ["Standards, planning, prioritizing, assessment", "The team's Mission"],
        ["Quality and mission reviews, classification", "The task's name"],
        ["Work", "The task's name and description"],
        ["Votes", "The proposed team's name"],
    ]
    return (
        '<section id="memory"><h2>Memory and learnings</h2>'
        "<p>DAASH does not search documents for agents. Files reach a prompt as a listing and "
        "as excerpts cut to fixed sizes (see Size limits). The only retrieval is over two "
        "small stores.</p>"
        + _table(["Store", "Written when", "Shared with"], [
            ["Learnings", "The retrospective writes 1 to 8 at the end of each swarm. A task "
             "that is Done after rework, or that fails, records the judges' feedback.",
             "Retrospective learnings: every future swarm, on purpose. Task learnings: the "
             "same swarm."],
            ["Memories", "A built-in agent calls its <code>remember</code> tool.",
             "Only that agent, through its system message and its <code>recall</code> tool."],
        ])
        + f"<p>For each job DAASH takes the {P._MAX_CONTEXT_ITEMS} learnings and "
          f"{P._MAX_CONTEXT_ITEMS} memories nearest to the job's subject, using pgvector "
          "cosine distance (newest first on ties). The subject depends on the job:</p>"
        + _table(["Job", "Matched against"], queries)
        + '<p class="note">With the default setting the vectors are word hashes, so '
          "&ldquo;nearest&rdquo; means &ldquo;shares the most words&rdquo;. Set "
          "<code>DAASH_EMBEDDING_USE_MODEL=true</code> to match by meaning with "
          "gte-base-en-v1.5. External agents cannot read or write memory yet.</p>"
        + "</section>")


def _jobs_section() -> str:
    jobs = _jobs()
    out = ['<section id="jobs"><h2>The jobs, by role</h2>'
           "<p>After the system message comes one job message. It starts with "
           "<code>Job:</code>, adds what that job needs to see, and ends with the exact JSON "
           "to reply with and one example. Each card shows when the job runs, what it adds, "
           "what the reply must contain, and what DAASH checks before using the reply.</p>"]
    for key, title, role_value, blurb in ROLES:
        model = _model(key)
        model_note = (f"starts at <code>{_e(model)}</code> and can move up" if key == "delivery"
                      else f"<code>{_e(model)}</code>")
        out.append(f'<div class="role" id="role-{key}"><div class="role-head">'
                   f'<h3 class="{key}">{_e(title)}</h3>'
                   f'<span class="chip {key}">default model {model_note}</span></div>'
                   f"<p>{_e(blurb)}</p>")
        for j in (j for j in jobs if j["role"] == key):
            adds = "".join(f"<li>{_e(a)}</li>" for a in j["adds"])
            out.append(
                f'<article class="job" id="job-{j["id"]}"><h3>{_e(j["name"])}</h3><dl>'
                f"<dt>When</dt><dd>{_e(j['when'])}</dd>"
                f"<dt>Adds</dt><dd><ul>{adds}</ul></dd>"
                f"<dt>Reply</dt><dd>{_e(j['reply'])}</dd>"
                f"<dt>Checked</dt><dd>{_e(j['checked'])}</dd></dl>"
                + _pre(j["example"](), "Example job message")
                + (_pre(j["example2"][0](), j["example2"][1]) if "example2" in j else "")
                + "</article>")
        out.append("</div>")
    out.append("</section>")
    return "".join(out)


def _tools_section() -> str:
    rows = []
    for spec in _tools():
        f = spec["function"]
        params = ", ".join(f["parameters"].get("properties", {}).keys()) or "none"
        rows.append([f"<code>{_e(f['name'])}</code>", _e(f["description"]),
                     f"<code>{_e(params)}</code>"])
    return (
        '<section id="tools"><h2>The built-in agent&rsquo;s tools</h2>'
        "<p>Built-in delivery agents work through these tools, offered as OpenAI function "
        "definitions. File tools cannot leave the swarm workspace. "
        "<code>vote_on_proposal</code> is offered only while a vote is waiting. "
        "<code>submit</code> ends the attempt.</p>"
        + _table(["Tool", "What it does", "Arguments"], rows)
        + "</section>")


def _cli_section() -> str:
    return (
        '<section id="external"><h2>External agents</h2>'
        "<p>OpenClaude, Claude Code and Codex agents run through a runner. DAASH sends the "
        "runner the system message and the job message. The runner adds the system message "
        "to the CLI's own system prompt and passes the job message on standard input.</p>"
        + _table(["Agent", "System message", "Job message", "Model"], [
            ["Claude Code, OpenClaude", "<code>--append-system-prompt</code>", "stdin, with "
             "<code>-p</code>", "<code>--model</code>"],
            ["Codex", "<code>-c developer_instructions=&hellip;</code>", "stdin, with "
             "<code>exec -</code>", "<code>-m</code>"],
        ])
        + "<p>External agents use their own tools for files and code. They do not get "
          "DAASH's memory, subtask or team-proposal tools yet.</p>"
        + "</section>")


def _rules_section() -> str:
    thr, lim = models.DoDThresholds(), models.Limits()
    limits = [
        ["Workspace file listing", f"{J.LISTING_LIMIT} files"],
        ["Each file shown to the review judges", f"{J.FILE_CLIP:,} characters"],
        ["Each file in the vision review", "4,000 characters (12 files)"],
        ["Mission assessment excerpts", f"{J.ASSESS_MAX_FILES} files, "
                                        f"{J.ASSESS_TOTAL_CHARS:,} characters in all"],
        ["Task lists in prompts", f"{_TASK_LIST_LIMIT} tasks; descriptions 200 characters"],
        ["read_file tool", f"{READ_LIMIT_CHARS:,} characters"],
        ["Transcripts kept for the dashboard", f"{TRANSCRIPT_CHARS:,} characters per message"],
        ["Binary files", "never shown; replaced by their size"],
    ]
    return (
        '<section id="rules"><h2>Rules for every prompt</h2>'
        '<h3 id="contract">The reply contract</h3>'
        "<p>Every job except the work itself ends with <code>Reply with ONLY one JSON object"
        "</code>, the exact shape and one example. DAASH finds the JSON even inside prose or "
        "code fences, accepts common variations (such as <code>\"95%\"</code> for 0.95), and "
        "checks the content. If the reply cannot be used, DAASH sends it back once with the "
        "reason:</p>"
        + _pre("Your reply could not be used: <the reason>\nReply again with ONLY the corrected "
               "JSON, following the contract exactly.", "The follow-up message")
        + _pre(NUDGE.format(tool="submit"), "The reminder to a built-in agent that answers "
                                            "without submitting")
        + '<h3 id="certainty">Certainty, not grades</h3>'
        f"<p>A judge's score is its certainty that a criterion is met. A rubric criterion "
        f"passes above {thr.rubric}; the mission value needs {thr.mission_confidence}. Any "
        "lower score must name a concrete, fixable defect, so the next attempt knows what to "
        "change.</p>"
        '<h3 id="rework">Rework feedback</h3>'
        "<p>When the judges send work back, everything they said goes into the next attempt's "
        "job message: failed checks with their evidence, every rubric criterion that scored "
        "short with its reason, and both judges' feedback.</p>"
        + _pre(_rework(), "Example: the rework part of a job message")
        + '<h3 id="scope">Scope and capabilities</h3>'
        "<p>In multi-team swarms, planning, prioritizing and assessment see the other teams' "
        "Missions so a team stays in its own lane. Work prompts tell agents to change only "
        "their own task's files. Work prompts state what the agent can and cannot do (edit "
        "files, run code, research the web), and the judges are told what the author could "
        "not do.</p>"
        + '<h3 id="models">Choosing the model</h3>'
        f"<p>Judges and the Swarm Coordinator use their configured models. Delivery work "
        f"starts at <code>{_e(_model('delivery'))}</code>, moves one step up the model ladder "
        f"for complex tasks, one more when past results show that step failing, and one more "
        f"for each failed attempt. Any job that gets {ESCALATE_AFTER} unusable replies in a "
        "row moves one step up.</p>"
        '<p class="note">DAASH picks the model only where it can. The built-in agent, Claude '
        "Code, OpenClaude and Codex accept a model from DAASH. An agent that DAASH reaches "
        "only by messages may not let it choose; for such agents, escalation will mean "
        "something else, such as handing the task to another team member.</p>"
        f"<p>Every built-in call uses temperature {_TEMPERATURE} and a reply budget of "
        f"{_MAX_TOKENS:,} tokens by default (<code>DAASH_LLM_MAX_TOKENS</code>).</p>"
        '<h3 id="limits">Size limits</h3>'
        + _table(["What", "Limit"], [[a, f'<span class="num">{_e(b)}</span>'] for a, b in limits])
        + "</section>")


def _code_section() -> str:
    rows = [
        ["Job messages and reply checks", "app/prompts.py"],
        ["System message and retrieval", "app/services.py (agent_system_prompt), app/repo.py"],
        ["Context: listings, files, rework feedback", "app/jobs.py"],
        ["Follow-up message, tool loop", "app/llm.py"],
        ["Built-in agent tools", "app/tools.py, app/jobs.py (_engine_tools)"],
        ["External agents", "daash_runner/adapters.py"],
        ["Model choice", "app/routing.py"],
        ["This page", "app/help.py"],
    ]
    return ('<section id="code"><h2>Where it lives in the code</h2>'
            + _table(["What", "Where"], [[a, f"<code>{_e(b)}</code>"] for a, b in rows])
            + "</section>")


def prompts_page(full_document: bool = False) -> str:
    """The help page. `full_document` adds the doctype/head for serving it directly."""
    toc = [("overview", "Two messages per call", False), ("system", "The system message", False),
           ("memory", "Memory and learnings", False), ("jobs", "The jobs, by role", False)]
    toc += [(f"role-{k}", t, True) for k, t, _, _ in ROLES]
    toc += [("tools", "Built-in agent tools", False), ("external", "External agents", False),
            ("rules", "Rules for every prompt", False), ("code", "Where it lives", False)]
    sub_cls = ' class="sub"'
    nav = "".join(f'<a href="#{i}"{sub_cls if sub else ""}>{_e(t)}</a>' for i, t, sub in toc)
    overview = (
        '<section id="overview"><h2>Two messages per call</h2>'
        "<p>Every model call DAASH makes for an agent has two messages. There are no template "
        "files: each job's message is built by a function in <code>app/prompts.py</code>, and "
        "a matching function checks the reply.</p>"
        '<div class="stack"><div class="msg"><h3>System message</h3><p>Who the agent is and '
        "what matters, from the top down.</p><ol><li>Vision</li><li>Values and Principles</li>"
        "<li>Mission</li><li>Definition of Done</li><li>Definition of Ready</li><li>Role</li>"
        "<li>User Feedback</li><li>Learnings</li><li>Memory</li></ol></div>"
        '<div class="msg"><h3>Job message</h3><p>What to do now.</p><ol><li><code>Job:</code> '
        "and its rules</li><li>What this job needs to see: tasks, files, listings, earlier "
        "feedback</li><li>The exact JSON to reply with, and one example</li></ol></div></div>"
        "<p>External agents (OpenClaude, Claude Code, Codex) receive the same two messages "
        "through a runner.</p></section>")
    body = (
        f'<div class="wrap"><header class="mast"><span class="eyebrow">DAASHboard help'
        f'</span><h1>How DAASH prompts its agents</h1><p class="lede">What each agent is told, '
        f"where every part comes from, and what DAASH checks in the reply. The examples are "
        f"rendered from DAASH's own prompt code with a sample prime-number swarm.</p></header>"
        f'<div class="layout"><nav class="toc" aria-label="Contents">{nav}</nav><main>'
        + overview + _system_section() + _retrieval_section() + _jobs_section()
        + _tools_section() + _cli_section() + _rules_section() + _code_section()
        + "</main></div></div>")
    head = (f"<title>{_e(TITLE)}</title>"
            '<meta name="claude:viewport-fit-sides" content="cover">'
            f"<style>{CSS}</style>")
    if not full_document:
        return head + body
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, '
            'viewport-fit=cover">' + head + "</head><body>" + body + "</body></html>")

