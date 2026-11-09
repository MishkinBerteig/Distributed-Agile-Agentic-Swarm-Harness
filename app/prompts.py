"""Prompts and output parsers for the DAASH agents (docs/DESIGN-0.1.md 7.2, 7.3).

* `compose_system_prompt` builds the deterministic alignment hierarchy
  (Vision > Values > Mission > DoD > DoR > Role > Feedback > Learnings > Memory).
* Each job has a user-message builder that states the exact JSON contract, and a
  parser that takes the already-extracted JSON value, validates and normalises it,
  and raises ValueError with a message the model can act on.
Pure module: no I/O.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app import models

# ---------------------------------------------------------------------------
# System prompt hierarchy
# ---------------------------------------------------------------------------

ROLE_TITLES = {
    "swarm_coordinator": "Swarm Coordinator",
    "mission_judgement": "Mission Judgement",
    "quality_judgement": "Quality Judgement",
    "mission_delivery": "Mission Delivery",
}

ROLE_JOBS = {
    "swarm_coordinator": (
        "You manage the whole swarm so that its Vision is accomplished. You turn the Vision "
        "into a vision-level Definition of Done and into teams, each with a Mission that "
        "directly refers to the Vision. After the swarm finishes you reflect on what it "
        "learned so future swarms improve."
    ),
    "mission_judgement": (
        "You monitor the work of the other agents in your team to make sure it stays aligned "
        "with the team's Mission. You approve which task is the next highest priority toward "
        "the Mission, you check whether the Mission has been accomplished (the expected value "
        "is met), and you call out a failure that requires learning and restarting the team."
    ),
    "quality_judgement": (
        "You monitor the work of the other agents in your team to make sure it meets ongoing "
        "quality expectations, including the Definition of Done. You set the team's quality "
        "standards and review deliverables against each acceptance criterion, with certainty "
        "and specific evidence."
    ),
    "mission_delivery": (
        "You work on tasks that accomplish your team's Mission. You divide work with the other "
        "Mission Delivery agents, produce real artifacts in the shared workspace, and decide by "
        "majority vote with them whether a new team with a new Mission is needed to accomplish "
        "some portion of the Mission."
    ),
}

_MAX_CONTEXT_ITEMS = 8


def _bullets(items: Iterable[str], limit: int | None = None) -> str:
    out = [str(i).strip() for i in items if str(i).strip()]
    if limit is not None:
        out = out[:limit]
    return "\n".join(f"- {i}" for i in out)


def compose_system_prompt(
    *,
    vision: str,
    role: str,
    values_principles: str = "",
    mission: Optional[str] = None,
    team_name: Optional[str] = None,
    vision_dod: Optional[str] = None,
    team_dor: Optional[str] = None,
    team_dod: Optional[str] = None,
    feedback: Iterable[str] = (),
    learnings: Iterable[str] = (),
    memories: Iterable[str] = (),
) -> str:
    """Deterministic hierarchy; empty sections are omitted."""
    sections: list[str] = [f"# Vision\n{vision.strip()}"]
    if values_principles and values_principles.strip():
        sections.append(f"# Values and Principles\n{values_principles.strip()}")
    if mission and mission.strip():
        head = f"# Mission\n"
        if team_name:
            head += f"(team: {team_name})\n"
        sections.append(head + mission.strip())
    dod_parts = []
    if vision_dod and vision_dod.strip():
        dod_parts.append(f"## Vision level\n{vision_dod.strip()}")
    if team_dod and team_dod.strip():
        dod_parts.append(f"## Team mission level\n{team_dod.strip()}")
    if dod_parts:
        sections.append("# Definition of Done\n" + "\n".join(dod_parts))
    if team_dor and team_dor.strip():
        sections.append(f"# Definition of Ready\n{team_dor.strip()}")
    title = ROLE_TITLES.get(role, role)
    job = ROLE_JOBS.get(role, "Contribute to the team according to your role.")
    sections.append(f"# Your Role: {title}\n{job}")
    fb = _bullets(feedback)
    if fb:
        sections.append(f"# User Feedback\n{fb}")
    ln = _bullets(learnings, _MAX_CONTEXT_ITEMS)
    if ln:
        sections.append(f"# Learnings\n{ln}")
    mm = _bullets(memories, _MAX_CONTEXT_ITEMS)
    if mm:
        sections.append(f"# Your Memory\n{mm}")
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

_JSON_ONLY = "Reply with ONLY one JSON object (no prose, no code fences). "


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _clip(s: Any, n: int) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + "...[truncated]"


def _as_obj(obj: Any, what: str = "reply") -> dict:
    if not isinstance(obj, dict):
        raise ValueError(f"the {what} must be a JSON object, got {type(obj).__name__}")
    return obj


def _as_list(obj: Any, key: str, what: str) -> list:
    """Accept {key: [...]} or a bare list."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, list):
            return v
        raise ValueError(f'the JSON object must have a "{key}" array ({what})')
    raise ValueError(f'expected a JSON object with a "{key}" array ({what})')


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return "\n".join(str(x).strip() for x in v if str(x).strip())
    return str(v).strip()


def _req_text(obj: dict, key: str, *aliases: str) -> str:
    for k in (key, *aliases):
        t = _text(obj.get(k))
        if t:
            return t
    raise ValueError(f'"{key}" is required and must be a non-empty string')


def _unit(v: Any, key: str, required: bool = True) -> Optional[float]:
    """Clamp to 0..1; accept numeric strings and percentages like '95%'."""
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValueError(f'"{key}" is required: a number from 0 to 1')
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        s = str(v).strip()
        pct = s.endswith("%")
        x = float(s.rstrip("%")) if isinstance(v, str) else float(v)
        if pct or 10.0 < x <= 100.0:  # '95' or '95%' on a 0..100 scale
            x /= 100.0
    except ValueError:
        raise ValueError(f'"{key}" must be a number from 0 to 1, got {v!r}') from None
    if x != x:  # NaN
        raise ValueError(f'"{key}" must be a number from 0 to 1')
    return max(0.0, min(1.0, x))


def _bool(v: Any, key: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "pass", "passed", "met", "1"):
            return True
        if s in ("false", "no", "fail", "failed", "not met", "0"):
            return False
    raise ValueError(f'"{key}" must be true or false')


def _crit_lines(criteria: Iterable[Any]) -> str:
    lines = []
    for c in criteria:
        kind = _get(c, "check_kind", "rubric")
        kind = getattr(kind, "value", kind)
        line = f"{_get(c, 'ordinal', '?')}. {_get(c, 'text', '')}"
        path, arg = _get(c, "check_path"), _get(c, "check_arg")
        if kind and kind != "rubric":
            line += f" [{kind} path={path}" + (f" arg={arg}" if arg else "") + "]"
        elif path:
            line += f" [rubric file={path}]"
        lines.append(line)
    return "\n".join(lines) or "(none)"


def _task_block(task: Any, with_criteria: bool = True) -> str:
    s = f"Task {_get(task, 'id', '')}: {_get(task, 'name', '')}\n"
    if _get(task, "description"):
        s += f"Description: {_get(task, 'description')}\n"
    if _get(task, "artifact"):
        s += f"Artifact: {_get(task, 'artifact')}\n"
    if with_criteria:
        s += "Acceptance criteria:\n" + _crit_lines(_get(task, "criteria", []) or [])
    return s.rstrip()


def _task_list(tasks: Iterable[Any], criteria: bool = False, limit: int = 30) -> str:
    out = []
    for t in list(tasks)[:limit]:
        line = f"- [{_get(t, 'id', '')}] {_get(t, 'name', '')}"
        if _get(t, "description"):
            line += f": {_clip(_get(t, 'description'), 200)}"
        if _get(t, "artifact"):
            line += f" (artifact: {_get(t, 'artifact')})"
        if _get(t, "status") and criteria:
            line += f" [{_get(t, 'status')}]"
        out.append(line)
    return "\n".join(out) or "(none)"


# ---------------------------------------------------------------------------
# Bootstrap (Swarm Coordinator)
# ---------------------------------------------------------------------------

@dataclass
class BootstrapResult:
    vision_dod: str
    teams: list[tuple[str, str]] = field(default_factory=list)  # (name, mission)


def bootstrap_prompt(max_teams: int, need_teams: bool) -> str:
    s = ("Job: bootstrap the swarm. Write the vision-level Definition of Done: 3 to 6 "
         "measurable, verifiable statements that together mean the Vision is accomplished.\n")
    if need_teams:
        s += (f"Also define 1 to {max_teams} teams. Each team has a short unique name and a "
              "Mission: one or two sentences that explicitly refer to the Vision and describe a "
              "concrete contribution (an operational or project-based one). Teams should "
              "not overlap.\n")
        s += _JSON_ONLY + 'Contract: {"vision_dod": "<statements separated by newlines>", ' \
             '"teams": [{"name": "...", "mission": "..."}]}\n'
        s += ('Example: {"vision_dod": "- A README explains the product\\n- All pages are '
              'proofread", "teams": [{"name": "Docs", "mission": "To advance the Vision, '
              'write the documentation..."}]}')
    else:
        s += "The teams already exist; do not define teams.\n"
        s += _JSON_ONLY + 'Contract: {"vision_dod": "<statements separated by newlines>"}\n'
        s += 'Example: {"vision_dod": "- A README explains the product\\n- All pages are proofread"}'
    return s


def parse_bootstrap(obj: Any, max_teams: int, need_teams: bool = False) -> BootstrapResult:
    o = _as_obj(obj)
    dod = _req_text(o, "vision_dod", "definition_of_done", "vision_definition_of_done")
    teams: list[tuple[str, str]] = []
    if max_teams > 0 and ("teams" in o or need_teams):
        raw = _as_list(o, "teams", "each item is {name, mission}")
        seen: set[str] = set()
        for i, t in enumerate(raw):
            if not isinstance(t, dict):
                raise ValueError(f'teams[{i}] must be an object {{"name": ..., "mission": ...}}')
            name = _text(t.get("name"))
            mission = _text(t.get("mission") or t.get("mission_statement"))
            if not name:
                raise ValueError(f'teams[{i}] needs a non-empty "name"')
            if len(mission) < 10:
                raise ValueError(f'teams[{i}] ("{name}") needs a "mission" sentence that refers to the Vision')
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            teams.append((name[:255], mission))
        teams = teams[:max_teams]
        if need_teams and not teams:
            raise ValueError('"teams" must contain at least one team')
    return BootstrapResult(vision_dod=dod, teams=teams)


# ---------------------------------------------------------------------------
# Standards (Quality Judgement): team DoR additions and team DoD
# ---------------------------------------------------------------------------

def standards_prompt(dor_guidance: str = "", dod_guidance: str = "") -> str:
    s = ("Job: set this team's quality standards.\n"
         "- definition_of_ready: team-specific readiness rules ONLY. The harness already enforces "
         "the generic ones (an idle delivery agent, Mission Judgement approval, at least one "
         "acceptance criterion, dependencies done), so do not repeat them.\n"
         "- definition_of_done: the bar every task of this team must clear, aligned with the "
         "Mission and the vision-level Definition of Done. Make each item checkable.\n")
    if dor_guidance.strip():
        s += f"User guidance for the Definition of Ready: {dor_guidance.strip()}\n"
    if dod_guidance.strip():
        s += f"User guidance for the Definition of Done: {dod_guidance.strip()}\n"
    s += (_JSON_ONLY + 'Contract: {"definition_of_ready": "<items separated by newlines>", '
          '"definition_of_done": "<items separated by newlines>"}\n'
          'Example: {"definition_of_ready": "- Names the file to change", '
          '"definition_of_done": "- Every claim is sourced\\n- No placeholder text"}')
    return s


def parse_standards(obj: Any) -> dict[str, str]:
    o = _as_obj(obj)
    dor, dod = _text(o.get("definition_of_ready")), _text(o.get("definition_of_done"))
    if not dod:
        raise ValueError('"definition_of_done" is required and must be a non-empty string')
    if "definition_of_ready" not in o:
        raise ValueError('"definition_of_ready" is required (a string, may be empty)')
    return {"definition_of_ready": dor, "definition_of_done": dod}


def extract_quality_json(text: str) -> dict[str, str]:
    """Parse a Quality Judgement response into {"definition_of_ready", "definition_of_done"}.

    Tolerates surrounding prose and ``` code fences. Raises ValueError when no
    JSON object with both string criteria can be recovered.
    """
    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)

    # Strip markdown code fences if present (```json ... ``` or ``` ... ```).
    fence_start = stripped.find("```")
    if fence_start != -1:
        body = stripped[fence_start + 3:]
        if body.lower().startswith("json"):
            body = body[4:]
        fence_end = body.find("```")
        candidates.append((body[:fence_end] if fence_end != -1 else body).strip())

    # Fall back to the first balanced {...} span in the text.
    start = stripped.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(stripped)):
            ch = stripped[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(stripped[start:i + 1])
                    break

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        dor = data.get("definition_of_ready")
        dod = data.get("definition_of_done")
        if isinstance(dor, str) and isinstance(dod, str) and (dor.strip() or dod.strip()):
            return {"definition_of_ready": dor.strip(), "definition_of_done": dod.strip()}

    raise ValueError(f"no valid quality JSON in model response: {text[:200]!r}")


# ---------------------------------------------------------------------------
# Planning (Mission Delivery)
# ---------------------------------------------------------------------------

def scope_block(other_teams: Iterable[Any]) -> str:
    """Sibling teams' Missions, so a team stays inside its own scope (multi-team swarms)."""
    lines = []
    for t in other_teams:
        lines.append(f'- "{_get(t, "name", "")}" ({_get(t, "status", "active")}): '
                     f'{_clip(_get(t, "mission_statement", ""), 300)}')
    if not lines:
        return ""
    return ("Other teams in this swarm own these Missions and the artifacts they produce. Stay "
            "inside YOUR team's Mission: do not plan, require, redo or rewrite their work. The "
            "vision-level Definition of Done is met by all teams together, not by your team "
            "alone.\n" + "\n".join(lines) + "\n\n")


@dataclass
class PlannedTask:
    name: str
    description: str
    artifact: str
    criteria: list[models.CriterionCreate]
    depends_on: list[int] = field(default_factory=list)  # 0-based indexes of earlier planned tasks


def plan_prompt(team_name: str, done_tasks: Iterable[Any], open_tasks: Iterable[Any],
                max_new: int, next_focus: Optional[str], workspace_listing: str,
                other_teams: Iterable[Any] = ()) -> str:
    s = (f'Job: plan the next tasks for team "{team_name}" toward its Mission.\n'
         f"Plan 1 to {max_new} NEW tasks. Do not repeat existing work.\n")
    s += scope_block(other_teams)
    if next_focus and next_focus.strip():
        s += f"Mission Judgement says to focus on: {next_focus.strip()}\n"
    s += (f"\nDone tasks:\n{_task_list(done_tasks)}\n\nOpen tasks:\n{_task_list(open_tasks)}\n\n"
          f"Workspace files:\n{workspace_listing.strip() or '(empty)'}\n\n"
          "Rules: each task changes the form, fit or function of one artifact (a file in the "
          "workspace, relative path) and is small enough for one sitting. Each task needs "
          "acceptance criteria that are each clearly green or red. Use at least one "
          "deterministic check per task. check_kind is one of:\n"
          "- file_exists (check_path)\n"
          "- file_contains (check_path, check_arg = regex)\n"
          "- min_words (check_path, check_arg = integer)\n"
          "- json_valid (check_path)\n"
          "- rubric (a judged quality criterion, no check_path needed)\n"
          "depends_on lists the 0-based positions of EARLIER tasks in your list that must be "
          "done first (usually empty).\n")
    s += (_JSON_ONLY + 'Contract: {"tasks": [{"name": "...", "description": "...", "artifact": '
          '"path/in/workspace.md", "acceptance_criteria": [{"text": "...", "check_kind": '
          '"file_exists", "check_path": "...", "check_arg": null}], "depends_on": []}]}\n'
          'Example: {"tasks": [{"name": "Write intro", "description": "Draft the introduction", '
          '"artifact": "intro.md", "acceptance_criteria": [{"text": "intro.md exists", '
          '"check_kind": "file_exists", "check_path": "intro.md"}, {"text": "intro.md has at '
          'least 150 words", "check_kind": "min_words", "check_path": "intro.md", "check_arg": '
          '"150"}, {"text": "The tone is clear and friendly", "check_kind": "rubric"}], '
          '"depends_on": []}]}')
    return s


def _safe_rel(p: Any) -> Optional[str]:
    if not isinstance(p, str):
        return None
    p = p.strip()
    if not p or p.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", p) or ".." in p.replace("\\", "/").split("/"):
        return None
    return p


def _parse_criterion(c: Any, where: str) -> models.CriterionCreate:
    if isinstance(c, str):
        c = {"text": c}
    if not isinstance(c, dict):
        raise ValueError(f"{where} must be an object with a \"text\"")
    text = _text(c.get("text") or c.get("criterion") or c.get("description"))
    if not text:
        raise ValueError(f'{where} needs a non-empty "text"')
    kind = str(c.get("check_kind") or "rubric").strip().lower()
    path = _safe_rel(c.get("check_path"))
    arg = c.get("check_arg")
    arg = None if arg is None or str(arg).strip() == "" else str(arg).strip()
    valid = {k.value for k in models.CheckKind}
    if kind not in valid:
        kind = "rubric"
    if kind != "rubric":
        ok = path is not None
        if ok and kind == "file_contains":
            try:
                re.compile(arg or "")
                ok = bool(arg)
            except re.error:
                ok = False
        if ok and kind == "min_words":
            try:
                ok = arg is not None and int(float(arg)) > 0
                arg = str(int(float(arg))) if ok else arg
            except ValueError:
                ok = False
        if not ok:  # downgrade: invalid check spec becomes a judged rubric criterion
            kind = "rubric"
    if kind in ("file_exists", "json_valid"):
        arg = None
    if kind == "rubric":
        arg = None
    return models.CriterionCreate(text=text, check_kind=models.CheckKind(kind),
                                  check_path=path, check_arg=arg)


def parse_plan(obj: Any, max_new: int) -> list[PlannedTask]:
    raw = _as_list(obj, "tasks", "each item is a task object")
    if not raw:
        raise ValueError('"tasks" is empty: plan at least one task')
    out: list[PlannedTask] = []
    for i, t in enumerate(raw[:max_new]):
        w = f"tasks[{i}]"
        if not isinstance(t, dict):
            raise ValueError(f"{w} must be an object")
        name = _text(t.get("name"))
        if not name:
            raise ValueError(f'{w} needs a non-empty "name"')
        crits_raw = t.get("acceptance_criteria", t.get("criteria"))
        if not isinstance(crits_raw, list) or not crits_raw:
            raise ValueError(f'{w} ("{name}") needs "acceptance_criteria": a non-empty array')
        crits = [_parse_criterion(c, f"{w}.acceptance_criteria[{j}]") for j, c in enumerate(crits_raw)]
        artifact = _safe_rel(t.get("artifact")) or ""
        if artifact and not any(c.check_kind.value in models.DETERMINISTIC_CHECKS for c in crits):
            crits.append(models.CriterionCreate(
                text=f"The file {artifact} exists", check_kind=models.CheckKind.FILE_EXISTS,
                check_path=artifact))
        deps_raw = t.get("depends_on") or []
        if not isinstance(deps_raw, list):
            raise ValueError(f'{w}.depends_on must be an array of earlier task positions')
        deps: list[int] = []
        for d in deps_raw:
            try:
                di = int(d)
            except (TypeError, ValueError):
                raise ValueError(f"{w}.depends_on has {d!r}; use 0-based integer positions of earlier tasks") from None
            if di < 0 or di >= i:
                raise ValueError(f"{w}.depends_on has {di}; it may only refer to EARLIER tasks (0..{i - 1})")
            if di not in deps:
                deps.append(di)
        out.append(PlannedTask(name=name[:255], description=_text(t.get("description")),
                               artifact=artifact, criteria=crits, depends_on=deps))
    return out


# ---------------------------------------------------------------------------
# Prioritisation (Mission Judgement)
# ---------------------------------------------------------------------------

@dataclass
class PrioritizeResult:
    order: list[str]
    next_task_id: Optional[str]
    rejections: list[tuple[str, str]] = field(default_factory=list)
    next_task_ids: list[str] = field(default_factory=list)
    # Mission Judgement approved nothing on purpose (`"next_task_ids": []`): the remaining
    # candidates belong to the Mission but cannot start yet, so they stay Pending.
    deferred: bool = False


def prioritize_prompt(candidates: Iterable[Any], done_tasks: Iterable[Any],
                      team_dor: Optional[str] = None, other_teams: Iterable[Any] = (),
                      slots: int = 1, workspace_files: str = "", can_wait: bool = True) -> str:
    slots = max(1, int(slots))
    s = scope_block(other_teams)
    s += ("Job: prioritize this team's candidate tasks by how directly they advance the Mission.\n"
         "1. order: ALL candidate task ids, highest priority first (respect dependencies).\n"
         f"2. next_task_ids: the tasks approved to start now: up to {slots} slot(s) are free, so "
         f"approve up to {slots} task(s), highest priority first, each not rejected and meeting "
         "the Definition of Ready. Approve fewer only if fewer candidates qualify. "
         "next_task_id: the first of them.")
    if can_wait:
        s += (" If no candidate can start yet (for example it needs work another team has not "
              "finished), approve none: \"next_task_ids\": [] and \"next_task_id\": null. Those "
              "tasks stay Pending and you will be asked again when more work is done.\n")
    else:
        s += (" No other work is in progress anywhere in this swarm, so waiting cannot change "
              "anything: approve at least one candidate that advances the Mission, or reject the "
              "ones that should not be done.\n")
    s += ("3. rejections: tasks that do not serve the Mission or the Vision, duplicate other "
          "work, or belong to another team's Mission, each with a reason. Rejected ids must not "
          "be in next_task_ids.")
    if can_wait:
        s += (" Never reject a task only because it cannot start yet: a rejected task is gone "
              "for good, so leave it Pending instead.")
    s += "\nCopy task ids exactly as written.\n\n"
    if team_dor and team_dor.strip():
        s += f"Team Definition of Ready:\n{team_dor.strip()}\n\n"
    cands = list(candidates)
    s += "Candidate tasks:\n"
    for t in cands:
        s += _task_block(t) + "\n"
    s += f"\nDone tasks:\n{_task_list(done_tasks)}\n\n"
    s += ("Files that exist now in the shared swarm workspace (all teams):\n"
          f"{workspace_files.strip() or '(none yet)'}\n\n")
    s += (_JSON_ONLY + 'Contract: {"order": ["<task id>", ...], "next_task_ids": ["<task id>", ...], '
          '"next_task_id": "<task id>" | null, '
          '"rejections": [{"task_id": "<task id>", "reason": "..."}]}\n'
          'Example: {"order": ["t2", "t1", "t3"], "next_task_ids": ["t2"], "next_task_id": "t2", '
          '"rejections": '
          '[{"task_id": "t3", "reason": "Unrelated to the Mission"}]}')
    return s


def parse_prioritize(obj: Any, valid_ids: Iterable[str], slots: int = 1,
                     allow_defer: bool = True) -> PrioritizeResult:
    slots = max(1, int(slots))
    o = _as_obj(obj)
    valid = [str(v) for v in valid_ids]
    vset = set(valid)

    def norm(v: Any, where: str) -> str:
        s = str(v).strip()
        if s not in vset:
            raise ValueError(f'{where} has unknown task id {s!r}; valid ids are: {", ".join(valid)}')
        return s

    raw_order = o.get("order", [])
    if not isinstance(raw_order, list):
        raise ValueError('"order" must be an array of task ids')
    order: list[str] = []
    for v in raw_order:
        s = norm(v, '"order"')
        if s not in order:
            order.append(s)
    rejections: list[tuple[str, str]] = []
    for i, r in enumerate(o.get("rejections") or []):
        if not isinstance(r, dict):
            raise ValueError(f'rejections[{i}] must be {{"task_id": ..., "reason": ...}}')
        tid = norm(r.get("task_id", r.get("id")), f"rejections[{i}]")
        reason = _text(r.get("reason")) or "Rejected by Mission Judgement"
        if all(tid != x for x, _ in rejections):
            rejections.append((tid, reason))
    rejected = {t for t, _ in rejections}
    raw_next: list[Any] = []
    ids_field = o.get("next_task_ids")
    if isinstance(ids_field, list):
        raw_next.extend(ids_field)
    elif ids_field not in (None, ""):
        raw_next.append(ids_field)
    legacy = o.get("next_task_id")
    if legacy is not None and str(legacy).strip().lower() not in ("", "null", "none"):
        raw_next.append(legacy)
    if not isinstance(ids_field, list) and legacy is not None and str(legacy).strip() in rejected:
        raise ValueError(f"next_task_id {legacy} is also in rejections; choose one task to approve")
    chosen: list[str] = []
    for v in raw_next:
        if v is None or str(v).strip().lower() in ("", "null", "none"):
            continue
        tid = norm(v, '"next_task_ids"')
        if tid not in rejected and tid not in chosen:
            chosen.append(tid)
    chosen = chosen[:slots]
    for v in valid:  # tasks the model forgot go last, keeping the order total
        if v not in order:
            order.append(v)
    # An explicit empty list approves nothing on purpose: the candidates wait (deferral).
    deferred = allow_defer and not chosen and not raw_next and isinstance(ids_field, list)
    if not chosen and not deferred:  # missing or conflicting answer: approve the first ones
        chosen = [v for v in order if v not in rejected][:slots]
    nxt = chosen[0] if chosen else None
    return PrioritizeResult(order=order, next_task_id=nxt, rejections=rejections,
                            next_task_ids=chosen,
                            deferred=deferred and any(v not in rejected for v in order))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_prompt(task: Any) -> str:
    return ("Job: classify how hard this task is for an AI agent writing files in a workspace.\n"
            "- simple: one small file or a short edit.\n"
            "- moderate: one substantial artifact or a few connected files.\n"
            "- complex: needs research-like reasoning, many files, or careful consistency.\n\n"
            f"{_task_block(task)}\n\n"
            + _JSON_ONLY + 'Contract: {"complexity": "simple" | "moderate" | "complex"}\n'
            'Example: {"complexity": "moderate"}')


def parse_classification(obj: Any) -> str:
    v = obj.get("complexity") if isinstance(obj, dict) else obj
    s = str(v or "").strip().lower()
    if s not in {c.value for c in models.Complexity}:
        raise ValueError('"complexity" must be exactly one of: "simple", "moderate", "complex"')
    return s


# ---------------------------------------------------------------------------
# Work (tool loop)
# ---------------------------------------------------------------------------

def work_prompt(task: Any, criteria: Iterable[Any], feedback: Optional[str],
                workspace_listing: str, pending_proposals: Iterable[Any] = ()) -> str:
    s = ("Job: do this task. Produce the artifact in the shared workspace using your tools "
         "(write_file, append_file, read_file, list_files). Work for real: write the actual "
         "content, never placeholders.\n\n")
    s += f"Task {_get(task, 'id', '')}: {_get(task, 'name', '')}\n"
    if _get(task, "description"):
        s += f"Description: {_get(task, 'description')}\n"
    if _get(task, "artifact"):
        s += f"Artifact to create or change: {_get(task, 'artifact')}\n"
    s += f"\nAcceptance criteria (each must be green):\n{_crit_lines(criteria)}\n"
    if feedback and feedback.strip():
        s += (f"\nThe previous attempt was rejected. Fix every point:\n{feedback.strip()}\n")
    s += f"\nExisting workspace files:\n{workspace_listing.strip() or '(empty)'}\n"
    props = list(pending_proposals)
    if props:
        s += "\nPending team proposals (vote with vote_on_proposal):\n"
        for p in props:
            s += (f"- {_get(p, 'id', '')}: {_get(p, 'name', '')} - "
                  f"{_clip(_get(p, 'mission_statement', ''), 200)}\n")
    s += ("\nBefore finishing, re-read your files and self-check EVERY acceptance criterion one "
          "by one; fix what is red. Then call the submit tool with a short summary of what you "
          "did and how each criterion is met. Do not stop without calling submit.")
    return s


# ---------------------------------------------------------------------------
# Quality review (Quality Judgement)
# ---------------------------------------------------------------------------

@dataclass
class QualityReview:
    passed: bool
    quality_score: float
    criteria: dict[int, tuple[bool, float, str]] = field(default_factory=dict)
    feedback: str = ""


_FILE_CLIP = 12000


def _files_block(files: dict[str, str]) -> str:
    if not files:
        return "(no files were produced)"
    return "\n".join(f"--- {p} ---\n{_clip(c, _FILE_CLIP)}\n" for p, c in files.items())


def quality_review_prompt(task: Any, criteria_with_det_results: Iterable[Any],
                          deliverable_files: dict[str, str]) -> str:
    lines, rubric = [], []
    for c in criteria_with_det_results:
        kind = _get(c, "check_kind", "rubric")
        kind = getattr(kind, "value", kind)
        o = _get(c, "ordinal", "?")
        if kind == "rubric":
            rubric.append(o)
            lines.append(f"{o}. [RUBRIC - you score it] {_get(c, 'text', '')}"
                         + (f" (file: {_get(c, 'check_path')})" if _get(c, "check_path") else ""))
        else:
            st = _get(c, "status", "unknown")
            ev = _get(c, "evidence")
            lines.append(f"{o}. [{kind}: automatically checked, result {str(st).upper()}"
                         + (f" - {ev}" if ev else "") + f"] {_get(c, 'text', '')}")
    s = (f"Job: review the delivered work for this task against the Definition of Done.\n"
         f"{_task_block(task, with_criteria=False)}\n\nCriteria:\n" + ("\n".join(lines) or "(none)")
         + f"\n\nDeliverable files:\n{_files_block(deliverable_files)}\n"
         "Each acceptance criterion is green or red with certainty. Score each RUBRIC criterion "
         "as your certainty that it is met: 1.0 = fully met and you cannot name any concrete "
         "defect against it; 0.5-0.9 = partly met; 0 = not met. Judge only what the criterion "
         "asks: do not deduct for style preferences or for improvements beyond it. Any score "
         "below 1.0 MUST name the specific, fixable defect in reason (quote or point to the "
         "file). Cite evidence from the files. Automatically checked criteria are already "
         "decided. quality_score uses the same scale for the work as a whole (1.0 when every "
         "criterion is fully met and nothing in the Definition of Done is violated). passed is "
         "true only if the work is complete and meets the Definition of Done. If not passed, "
         "feedback must list concrete fixes.\n")
    ex_ord = rubric[0] if rubric else 1
    s += (_JSON_ONLY + 'Contract: {"passed": true|false, "quality_score": 0.0-1.0, "criteria": '
          '[{"ordinal": <int>, "passed": true|false, "score": 0.0-1.0, "reason": "..."}], '
          '"feedback": "..."}\n'
          f'Include one entry in "criteria" for each RUBRIC ordinal ({", ".join(map(str, rubric)) or "none"}).\n'
          f'Example: {{"passed": true, "quality_score": 1.0, "criteria": [{{"ordinal": {ex_ord}, '
          '"passed": true, "score": 1.0, "reason": "Section 2 states the rule clearly"}], '
          '"feedback": ""}')
    return s


def parse_quality_review(obj: Any, rubric_ordinals: Iterable[int]) -> QualityReview:
    o = _as_obj(obj)
    need = [int(x) for x in rubric_ordinals]
    got: dict[int, tuple[bool, float, str]] = {}
    raw = o.get("criteria", [])
    if isinstance(raw, dict):
        raw = [{"ordinal": k, **(v if isinstance(v, dict) else {"score": v})} for k, v in raw.items()]
    if not isinstance(raw, list):
        raise ValueError('"criteria" must be an array of {ordinal, passed, score, reason}')
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            raise ValueError(f"criteria[{i}] must be an object")
        try:
            ordn = int(c.get("ordinal"))
        except (TypeError, ValueError):
            raise ValueError(f'criteria[{i}] needs an integer "ordinal"') from None
        if ordn not in need:
            continue
        score = _unit(c.get("score"), f"criteria[{i}].score", required=False)
        passed_raw = c.get("passed")
        if passed_raw is None and score is None:
            raise ValueError(f'criteria[{i}] (ordinal {ordn}) needs "score" and "passed"')
        passed = _bool(passed_raw, f"criteria[{i}].passed") if passed_raw is not None else (score or 0) >= 0.5
        if score is None:
            score = 1.0 if passed else 0.0
        got[ordn] = (passed, score, _text(c.get("reason")))
    missing = [n for n in need if n not in got]
    if missing:
        raise ValueError(f'"criteria" is missing rubric ordinals {missing}; give a score for each')
    qs = _unit(o.get("quality_score", o.get("score")), "quality_score", required=False)
    if qs is None:
        if not got:
            raise ValueError('"quality_score" is required: a number from 0 to 1')
        qs = sum(s for _, s, _ in got.values()) / len(got)
    if "passed" not in o:
        raise ValueError('"passed" is required: true or false')
    return QualityReview(passed=_bool(o["passed"], "passed"), quality_score=qs, criteria=got,
                         feedback=_text(o.get("feedback")))


# ---------------------------------------------------------------------------
# Mission review (Mission Judgement)
# ---------------------------------------------------------------------------

@dataclass
class MissionReview:
    expected_value_met: bool
    confidence: float
    reasoning: str = ""
    feedback: str = ""


def mission_review_prompt(task: Any, deliverable_summary: str,
                          deliverable_files: dict[str, str]) -> str:
    return ("Job: judge whether this delivered work meets the expected value of the task for the "
            "team's Mission and the Vision. Ignore formatting nitpicks (another judge covers "
            "quality); decide if the work actually advances the Mission.\n\n"
            f"{_task_block(task)}\n\nAgent's summary:\n{_clip(deliverable_summary, 2000) or '(none)'}\n\n"
            f"Deliverable files:\n{_files_block(deliverable_files)}\n"
            "confidence is your probability (0 to 1) that the expected value is met. If you "
            "cannot name a concrete gap between the work and what the task and Mission require, "
            "the value is met: answer 0.99 or 1.0. Below 0.99 you MUST name the concrete gap in "
            "feedback, as a fix the delivery agent can make.\n"
            + _JSON_ONLY + 'Contract: {"expected_value_met": true|false, "confidence": 0.0-1.0, '
            '"reasoning": "...", "feedback": "..."}\n'
            'Example: {"expected_value_met": true, "confidence": 0.99, "reasoning": "The guide '
            'covers all three steps the Mission requires", "feedback": ""}')


#: Mission Judgement must name a concrete gap whenever it is less than this sure
#: (README: the expected value is met with 99% confidence or better).
MISSION_CONFIDENCE_FLOOR = 0.99


def parse_mission_review(obj: Any, min_confidence: float = MISSION_CONFIDENCE_FLOOR
                         ) -> MissionReview:
    o = _as_obj(obj)
    if "expected_value_met" not in o:
        raise ValueError('"expected_value_met" is required: true or false')
    r = MissionReview(
        expected_value_met=_bool(o["expected_value_met"], "expected_value_met"),
        confidence=_unit(o.get("confidence"), "confidence"),  # type: ignore[arg-type]
        reasoning=_text(o.get("reasoning")), feedback=_text(o.get("feedback")))
    # Enforce the contract: a verdict that blocks Done must say what to fix, or the
    # delivery agent reworks blind. Re-asked once by complete_json.
    if (not r.expected_value_met or r.confidence < min_confidence) and not r.feedback:
        raise ValueError(
            f'"feedback" is empty, but your verdict (expected_value_met={str(r.expected_value_met).lower()}, '
            f'confidence={r.confidence:.2f}) would send the work back. Either name the specific, '
            f'fixable gap in "feedback", or, if you cannot name one, set expected_value_met to '
            f'true and confidence to {min_confidence} or higher.')
    return r


# ---------------------------------------------------------------------------
# Team proposal vote (Mission Delivery)
# ---------------------------------------------------------------------------

def vote_prompt(proposal: Any, team_name: str, open_tasks: Iterable[Any],
                done_tasks: Iterable[Any]) -> str:
    reason = _get(proposal, "reason", "") or "(none given)"
    return ("Job: vote on a proposal to spin out a NEW team with its own Mission. Approve only if "
            "the proposed Mission is a distinct portion of the Vision that your team's current "
            "Mission does not cover well, and a separate team would accomplish it better than "
            "your team alone. Reject duplicates, overlaps and busywork. A majority of your "
            "team's Mission Delivery agents decides.\n"
            f"Your team: {team_name}\n"
            f'Proposed team: {_get(proposal, "name", "")}\n'
            f'Proposed Mission: {_get(proposal, "mission_statement", "")}\n'
            f"Proposer's reason: {_clip(reason, 600)}\n\n"
            f"Your team's open tasks:\n{_task_list(open_tasks)}\n\n"
            f"Your team's done tasks:\n{_task_list(done_tasks)}\n"
            + _JSON_ONLY + 'Contract: {"approve": true|false, "reason": "one sentence"}\n'
            'Example: {"approve": false, "reason": "Our current Mission already covers this"}')


def parse_vote(obj: Any) -> tuple[bool, str]:
    o = _as_obj(obj)
    if "approve" not in o:
        raise ValueError('"approve" is required: true or false')
    return _bool(o["approve"], "approve"), _text(o.get("reason"))


# ---------------------------------------------------------------------------
# Mission assessment (Mission Judgement)
# ---------------------------------------------------------------------------

@dataclass
class Assessment:
    status: str  # continue | accomplished | restart
    confidence: float
    reasoning: str = ""
    next_focus: str = ""


def assess_prompt(team: Any, done_tasks: Iterable[Any], failed_tasks: Iterable[Any],
                  other_teams: Iterable[Any] = ()) -> str:
    return (scope_block(other_teams) + "Job: assess the team's Mission now that it has no open tasks. "
            "Judge it against the team's Mission only.\n"
            f'Team: {_get(team, "name", "")}\nMission: {_get(team, "mission_statement", "")}\n\n'
            f"Done tasks:\n{_task_list(done_tasks)}\n\nFailed tasks:\n{_task_list(failed_tasks)}\n\n"
            "Choose status:\n"
            "- accomplished: the Mission's expected value is fully met by the done work.\n"
            "- continue: more work is needed; next_focus says what the next tasks should do.\n"
            "- restart: the approach is failing; learn from it and start over; next_focus says "
            "what to do differently.\n"
            "confidence is your probability that the Mission's expected value is met. If you "
            "cannot name specific missing or wrong content in the team's Mission scope, it is "
            "met: say accomplished with confidence 0.99 or more. General polish (tightening, "
            "rewording, clarity passes) is not missing content. Otherwise say continue and name "
            "that content in next_focus.\n"
            + _JSON_ONLY + 'Contract: {"status": "continue"|"accomplished"|"restart", '
            '"confidence": 0.0-1.0, "reasoning": "...", "next_focus": "..."}\n'
            'Example: {"status": "continue", "confidence": 0.8, "reasoning": "Intro is done, '
            'body is missing", "next_focus": "Write the body sections"}')


def parse_assessment(obj: Any) -> Assessment:
    o = _as_obj(obj)
    st = _text(o.get("status")).lower()
    if st not in ("continue", "accomplished", "restart"):
        raise ValueError('"status" must be exactly one of: "continue", "accomplished", "restart"')
    a = Assessment(status=st, confidence=_unit(o.get("confidence"), "confidence"),  # type: ignore[arg-type]
                   reasoning=_text(o.get("reasoning")), next_focus=_text(o.get("next_focus")))
    # Same contract as the review judges: anything short of "accomplished at >= 0.99"
    # must name the concrete remaining work, or the team plans vague polish forever.
    blocking = a.status != "accomplished" or a.confidence < MISSION_CONFIDENCE_FLOOR
    if blocking and not a.next_focus:
        raise ValueError(
            f'"next_focus" is empty, but status "{a.status}" at confidence {a.confidence:.2f} '
            "means more work. Either name the specific missing or wrong content in "
            '"next_focus" (status "continue"), or, if you cannot name any, answer status '
            f'"accomplished" with confidence {MISSION_CONFIDENCE_FLOOR} or higher.')
    return a


# ---------------------------------------------------------------------------
# Retrospective (Swarm Coordinator)
# ---------------------------------------------------------------------------

def retrospective_prompt(summary: str) -> str:
    return ("Job: run a retrospective on the finished swarm. Write 1 to 8 learnings that would help "
            "FUTURE swarms (they are injected into their prompts), each one short, concrete and "
            "reusable. kind is one of:\n"
            "- pattern: a practice that improves value, quality or speed\n"
            "- automation: a deterministic script or procedure worth automating to save tokens\n"
            "- routing: which model or tier suits which kind of work\n\n"
            f"Swarm history:\n{_clip(summary, 12000)}\n\n"
            + _JSON_ONLY + 'Contract: {"learnings": [{"kind": "pattern"|"automation"|"routing", '
            '"content": "..."}]}\n'
            'Example: {"learnings": [{"kind": "pattern", "content": "Give every task a '
            'file_exists check so completion is verifiable"}]}')


def parse_retrospective(obj: Any) -> list[tuple[str, str]]:
    raw = _as_list(obj, "learnings", "each item is {kind, content}")
    kinds = {k.value for k in models.LearningKind}
    out: list[tuple[str, str]] = []
    for i, it in enumerate(raw):
        if isinstance(it, str):
            it = {"kind": "pattern", "content": it}
        if not isinstance(it, dict):
            raise ValueError(f'learnings[{i}] must be {{"kind": ..., "content": ...}}')
        content = _text(it.get("content"))
        if not content:
            raise ValueError(f'learnings[{i}] needs non-empty "content"')
        kind = _text(it.get("kind")).lower()
        out.append((kind if kind in kinds else "pattern", content))
    if not out:
        raise ValueError('"learnings" is empty: give at least one learning')
    return out[:8]
