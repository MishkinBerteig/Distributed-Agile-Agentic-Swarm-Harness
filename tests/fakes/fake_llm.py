"""Scripted, deterministic fake of the OpenAI-compatible LLM gateway (docs/DESIGN-0.2.md A.1).

`FakeLLM` answers every DAASH job with contract-valid output and nothing else: it reads
the system prompt's ``# Your Role:`` line and the first user message's ``Job:`` line to
tell which job is asking, then replies from the *public prompt contracts* (the JSON shapes
and the ``_crit_lines`` criterion format in app/prompts.py). It never imports engine
internals. Work tasks get real tool calls: ``write_file`` content that satisfies the task's
deterministic acceptance criteria, then ``submit``.

Three ways to use it:

* in-process, no network: ``LLMClient(url, transport=httpx.MockTransport(fake.httpx_handler))``;
* over HTTP: ``python -m tests.fakes.fake_llm_server --port 9100`` (see fake_llm_server.py);
* directly: ``fake.handle(request_dict) -> response_dict`` (pure, synchronous, no delay).

Scenarios change behaviour at run time (``configure({...})``; over HTTP ``POST
/control/scenario``). ``reset()`` restores the happy path. To add a scenario, add a field
to ``Scenario`` and read it where the answer is built (``_answer_*``); ``configure`` and the
HTTP control endpoint pick it up automatically.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Optional

import httpx

DEFAULT_MODELS = ["instant", "fast", "great", "amazing"]
REASK_MARKER = "Your reply could not be used"


class FakeHTTPError(Exception):
    """An HTTP failure the fake wants the client to see (outage)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.body = {"error": {"message": message, "type": "fake_outage"}}


@dataclass
class Scenario:
    """Everything that can be varied. The defaults are the happy path."""

    teams: int = 1                       # teams the coordinator defines when asked
    team_names: list = field(default_factory=list)   # explicit names; default "Team A", ...
    tasks_per_plan: int = 2              # tasks per plan reply (capped by the prompt's limit)
    plan_rounds: int = 1                 # plan replies per team before MJ says "accomplished"
    plan_dependencies: str = "none"      # "none" | "chain" (each task depends on the previous)
    review_fail_attempts: int = 0        # judges block the first N review attempts of each task
    review_fail_judge: str = "quality"   # "quality" | "mission" | "both"
    accomplished_confidence: float = 0.995   # MJ confidence when it says "accomplished"
    assess_restart_count: int = 0        # next N assessments answer "restart"
    empty_feedback_first: int = 0        # next N review_mission/assess replies block without
                                         # naming a gap (parser rejects; the re-ask is valid)
    outage_seconds: float = 0.0          # set to N: every call gets HTTP outage_status for N s
    outage_status: int = 503
    malformed_next: int = 0              # next N calls reply with text that contains no JSON
    fail_after_calls: int = -1           # N >= 0: allow N more calls, then outage until set to -1
    delay_ms: int = 0                    # added latency per call
    vote_approve: bool = True            # answer to team-proposal votes
    work_submit: bool = True             # False: work agents answer in text and never submit
    work_propose_team: str = ""          # one-shot: the next work reply also calls propose_team
    work_remember: bool = False          # the work reply also calls remember
    reject_names_containing: str = ""    # prioritize rejects candidates whose name has this
    defer_prioritize: int = 0            # next N prioritize replies approve nothing ([])
    record_prompts: bool = False         # keep the system and first user prompt in call records
    complexity: str = "simple"           # answer to classification
    prompt_tokens: int = 100             # usage reported per call
    completion_tokens: int = 50
    models: list = field(default_factory=lambda: list(DEFAULT_MODELS))


# -- prompt contracts (mirrors app/prompts.py wording; kept loose on purpose) ----------

_JOBS: list[tuple[str, str]] = [
    ("bootstrap", r"^Job: bootstrap"),
    ("standards", r"^Job: set this team's quality standards"),
    ("plan", r"^Job: plan "),
    ("prioritize", r"^Job: prioritiz"),
    ("classify", r"^Job: classify"),
    ("work", r"^Job: do this task"),
    ("review_quality", r"^Job: review the delivered work"),
    ("review_mission", r"^Job: judge whether this delivered work"),
    ("vote", r"^Job: vote on a proposal"),
    ("assess", r"^Job: assess"),
    ("retrospective", r"^Job: run a retrospective"),
]
_JOB_RES = [(name, re.compile(rx, re.MULTILINE)) for name, rx in _JOBS]
_ROLE_RE = re.compile(r"^# Your Role: (.+)$", re.MULTILINE)
_TEAM_IN_SYSTEM_RE = re.compile(r"^# Mission\n\(team: (.+?)\)", re.MULTILINE)
_TASK_RE = re.compile(r"^Task (\S+?): (.*)$", re.MULTILINE)
_CRIT_RE = re.compile(
    r"^(\d+)\. (.*?) \[(file_exists|file_contains|min_words|json_valid) path=(\S+?)"
    r"(?: arg=(.*?))?\]$")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def detect_job(user_text: str) -> str:
    for name, rx in _JOB_RES:
        if rx.search(user_text):
            return name
    return "unknown"


def detect_role(system_text: str) -> str:
    m = _ROLE_RE.search(system_text)
    return m.group(1).strip() if m else ""


def _msg_text(m: dict) -> str:
    c = m.get("content")
    return c if isinstance(c, str) else ""


def _sample_for_regex(pattern: str) -> str:
    """A string the (case-insensitive, multiline) regex matches; works for plain words."""
    stripped = re.sub(r"\\b|\^|\$", "", pattern)
    for cand in (pattern, stripped, re.sub(r"\\(.)", r"\1", stripped)):
        if re.search(pattern, cand, re.IGNORECASE | re.MULTILINE):
            return cand
    return pattern


def parse_criteria(prompt: str) -> list[dict]:
    """Deterministic criteria from a work prompt's 'Acceptance criteria' block."""
    out: list[dict] = []
    marker = "Acceptance criteria (each must be green):\n"
    i = prompt.find(marker)
    if i < 0:
        return out
    block = prompt[i + len(marker):].split("\n\n", 1)[0]
    for line in block.splitlines():
        m = _CRIT_RE.match(line.strip())
        if m:
            out.append({"ordinal": int(m.group(1)), "text": m.group(2), "kind": m.group(3),
                        "path": m.group(4), "arg": m.group(5)})
    return out


def build_file_content(path: str, needs: dict, title: str) -> str:
    """File text meeting min_words, file_contains (regex) and json_valid for one path."""
    parts = [f"# {title}", ""]
    for pat in needs.get("contains", []):
        parts.append(_sample_for_regex(pat))
    min_words = needs.get("min_words", 0)
    sentence = "This section explains the deliverable clearly and completely for the reader."
    parts.append(sentence)
    while len(" ".join(parts).split()) < min_words + 8:
        parts.append(sentence)
    body = "\n".join(parts)
    if needs.get("json") or path.lower().endswith(".json"):
        return json.dumps({"title": title, "text": body})
    return body + "\n"


# -- the fake ------------------------------------------------------------------------

class FakeLLM:
    def __init__(self, scenario: Optional[dict] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.reset()
        if scenario:
            self.configure(scenario)

    # -- control -----------------------------------------------------------------

    def reset(self) -> None:
        """Back to the happy path; forget all counters and recorded calls."""
        self.scenario = Scenario()
        self.calls: list[dict] = []
        self._seq = 0
        self._outage_until: Optional[float] = None
        self._call_budget: Optional[int] = None
        self._plan_calls: dict[str, int] = {}
        self._task_index: dict[str, int] = {}
        self._review_attempts: dict[tuple, int] = {}
        self._proposed_once = False

    def configure(self, settings: dict) -> dict:
        """Merge scenario settings. Unknown keys raise ValueError. Returns the scenario."""
        known = {f.name: f for f in fields(Scenario)}
        unknown = sorted(set(settings) - set(known))
        if unknown:
            raise ValueError(f"unknown scenario key(s): {', '.join(unknown)}; "
                             f"valid keys: {', '.join(sorted(known))}")
        for key, value in settings.items():
            cur = getattr(self.scenario, key)
            if isinstance(cur, bool):
                value = (value.strip().lower() in ("1", "true", "yes")
                         if isinstance(value, str) else bool(value))
            elif isinstance(cur, int):
                value = int(value)
            elif isinstance(cur, float):
                value = float(value)
            elif isinstance(cur, list):
                value = list(value)
            elif isinstance(cur, str):
                value = "" if value is None else str(value)
            setattr(self.scenario, key, value)
            if key == "outage_seconds":
                self._outage_until = (self._clock() + float(value)) if float(value) > 0 else None
            if key == "fail_after_calls":
                self._call_budget = int(value) if int(value) >= 0 else None
            if key == "work_propose_team":
                self._proposed_once = False
        return self.scenario_dict()

    def scenario_dict(self) -> dict:
        return copy.deepcopy(asdict(self.scenario))

    def calls_for(self, job: str) -> list[dict]:
        return [c for c in self.calls if c["job"] == job]

    def max_overlap(self, job: str, model: Optional[str] = None) -> int:
        """Largest number of calls of `job` (optionally only on `model`) in flight at once."""
        events: list[tuple[float, int]] = []
        for c in self.calls_for(job):
            if c.get("t_end") is None or (model is not None and c["model"] != model):
                continue
            events.append((c["t"], 1))
            events.append((c["t_end"], -1))
        best = cur = 0
        for _, d in sorted(events, key=lambda e: (e[0], e[1])):
            cur += d
            best = max(best, cur)
        return best

    # -- transports ----------------------------------------------------------------

    def models_response(self) -> tuple[int, dict]:
        if self._in_outage():
            return self.scenario.outage_status, {"error": {"message": "fake outage"}}
        return 200, {"object": "list",
                     "data": [{"id": m, "object": "model", "owned_by": "fake"}
                              for m in self.scenario.models]}

    async def httpx_handler(self, request: httpx.Request) -> httpx.Response:
        """Handler for ``httpx.MockTransport``."""
        path = request.url.path
        if request.method == "GET" and path.endswith("/models"):
            status, data = self.models_response()
            return httpx.Response(status, json=data)
        if request.method == "POST" and path.endswith("/chat/completions"):
            status, data = await self.ahandle(json.loads(request.content))
            return httpx.Response(status, json=data)
        return httpx.Response(404, json={"error": {"message": f"not found: {path}"}})

    async def ahandle(self, request: dict) -> tuple[int, dict]:
        """Like ``handle`` but with the scenario's delay; returns (status, body)."""
        rec: Optional[dict] = None
        try:
            rec = self._begin(request)
            if self.scenario.delay_ms > 0:
                await asyncio.sleep(self.scenario.delay_ms / 1000.0)
            return 200, self._answer(request, rec)
        except FakeHTTPError as exc:
            if rec is not None:
                rec["status"] = exc.status
            return exc.status, exc.body
        except asyncio.CancelledError:
            if rec is not None:
                rec["cancelled"] = True
            raise
        finally:
            if rec is not None:
                rec["t_end"] = time.time()

    def handle(self, request: dict) -> dict:
        """Answer one chat-completion request (dict in, dict out). Raises FakeHTTPError
        during an outage. No delay."""
        rec = self._begin(request)
        try:
            return self._answer(request, rec)
        finally:
            rec["t_end"] = time.time()

    # -- internals -----------------------------------------------------------------

    def _in_outage(self) -> bool:
        return self._outage_until is not None and self._clock() < self._outage_until

    def _begin(self, request: dict) -> dict:
        messages = request.get("messages") or []
        system = next((_msg_text(m) for m in messages if m.get("role") == "system"), "")
        user = next((_msg_text(m) for m in messages if m.get("role") == "user"), "")
        job = detect_job(user)
        team = _TEAM_IN_SYSTEM_RE.search(system)
        task = _TASK_RE.search(user)
        self._seq += 1
        rec = {"seq": self._seq, "t": time.time(), "t_end": None,
               "model": request.get("model", ""), "job": job, "role": detect_role(system),
               "team": team.group(1) if team else None,
               "task_id": task.group(1) if task and job in (
                   "work", "review_quality", "review_mission", "prioritize") else None,
               "status": 200, "cancelled": False, "has_tools": bool(request.get("tools")),
               "feedback_in_prompt": "The previous attempt was rejected" in user,
               "reask": REASK_MARKER in "\n".join(
                   _msg_text(m) for m in messages if m.get("role") == "user"),
               "prompt_tokens": self.scenario.prompt_tokens,
               "completion_tokens": self.scenario.completion_tokens,
               "total_tokens": self.scenario.prompt_tokens + self.scenario.completion_tokens}
        if self.scenario.record_prompts:
            rec["system"], rec["user"] = system, user
        self.calls.append(rec)
        if self._call_budget is not None:
            if self._call_budget <= 0:
                rec["status"] = self.scenario.outage_status
                raise FakeHTTPError(self.scenario.outage_status, "fake outage: call budget used")
            self._call_budget -= 1
        if self._in_outage():
            rec["status"] = self.scenario.outage_status
            raise FakeHTTPError(self.scenario.outage_status, "fake outage: gateway unavailable")
        return rec

    def _usage(self) -> dict:
        p, c = self.scenario.prompt_tokens, self.scenario.completion_tokens
        return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}

    def _reply(self, request: dict, message: dict, finish: str) -> dict:
        return {"id": f"chatcmpl-fake-{self._seq}", "object": "chat.completion",
                "created": int(time.time()), "model": request.get("model", "fake"),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": self._usage()}

    def _text(self, request: dict, text: str) -> dict:
        return self._reply(request, {"role": "assistant", "content": text,
                                     "reasoning_content": "thinking (fake)"}, "stop")

    def _json(self, request: dict, obj: Any) -> dict:
        return self._text(request, json.dumps(obj))

    def _tool_calls(self, request: dict, calls: list[tuple[str, dict]]) -> dict:
        tcs = [{"id": f"call_{self._seq}_{i}", "type": "function",
                "function": {"name": n, "arguments": json.dumps(a)}}
               for i, (n, a) in enumerate(calls)]
        return self._reply(request, {"role": "assistant", "content": None, "tool_calls": tcs},
                           "tool_calls")

    def _answer(self, request: dict, rec: dict) -> dict:
        s = self.scenario
        messages = request.get("messages") or []
        job = rec["job"]
        if s.malformed_next > 0:
            s.malformed_next -= 1
            return self._text(request, "I am sorry, but I would rather not answer in JSON.")
        user_msgs = [m for m in messages if m.get("role") == "user"]
        user = _msg_text(user_msgs[0]) if user_msgs else ""
        fresh = not rec["reask"]
        handler = getattr(self, f"_answer_{job}", None)
        if handler is None:
            return self._text(request, "OK")
        return handler(request, messages, user, fresh, rec)

    # -- bootstrap / standards ------------------------------------------------------

    def _team_name(self, i: int) -> str:
        names = self.scenario.team_names
        return names[i] if i < len(names) else f"Team {chr(65 + i)}"

    def _answer_bootstrap(self, request, messages, user, fresh, rec):
        out: dict[str, Any] = {"vision_dod": (
            "- Every deliverable file exists and is complete\n"
            "- Each part meets the Vision as written\n"
            "- Nothing is left as a placeholder")}
        m = re.search(r"Also define 1 to (\d+) teams", user)
        if m:
            n = max(1, min(int(m.group(1)), self.scenario.teams))
            out["teams"] = [
                {"name": self._team_name(i),
                 "mission": f"To advance the Vision, {self._team_name(i)} produces part {i + 1} "
                            f"of the deliverables the Vision asks for."}
                for i in range(n)]
        return self._json(request, out)

    def _answer_standards(self, request, messages, user, fresh, rec):
        return self._json(request, {
            "definition_of_ready": "- The task names the file it changes",
            "definition_of_done": "- Every file is complete and has no placeholder text\n"
                                  "- Each acceptance criterion is verifiably met"})

    # -- plan / prioritize / classify -----------------------------------------------

    def _answer_plan(self, request, messages, user, fresh, rec):
        s = self.scenario
        m = re.search(r'plan the next tasks for team "(.+?)"', user)
        team = m.group(1) if m else "team"
        cap_m = re.search(r"Plan 1 to (\d+) NEW", user)
        n = max(1, min(s.tasks_per_plan, int(cap_m.group(1)) if cap_m else s.tasks_per_plan))
        if fresh:
            self._plan_calls[team] = self._plan_calls.get(team, 0) + 1
        slug = _slug(team)
        tasks = []
        for i in range(n):
            self._task_index[team] = self._task_index.get(team, 0) + 1
            k = self._task_index[team]
            art = f"{slug}-part-{k}.md"
            tasks.append({
                "name": f"{team} task {k}",
                "description": f"Write part {k} of the deliverables for {team}.",
                "artifact": art,
                "acceptance_criteria": [
                    {"text": f"{art} exists", "check_kind": "file_exists", "check_path": art},
                    {"text": f"{art} has at least 20 words", "check_kind": "min_words",
                     "check_path": art, "check_arg": "20"},
                    {"text": f"{art} has an Overview", "check_kind": "file_contains",
                     "check_path": art, "check_arg": "Overview"},
                    {"text": "The content is clear and complete", "check_kind": "rubric"}],
                "depends_on": [i - 1] if (s.plan_dependencies == "chain" and i > 0) else []})
        return self._json(request, {"tasks": tasks})

    def _answer_prioritize(self, request, messages, user, fresh, rec):
        s = self.scenario
        block = user.split("Candidate tasks:", 1)[-1].split("\nDone tasks:", 1)[0]
        cands = [(m.group(1), m.group(2)) for m in _TASK_RE.finditer(block)]
        slots = 1
        for rx in (r"up to (\d+) slot", r"(\d+) slot", r"slots?\D{0,30}(\d+)"):
            sm = re.search(rx, user)
            if sm:
                slots = max(1, int(sm.group(1)))
                break
        rejected = [(tid, name) for tid, name in cands
                    if s.reject_names_containing and s.reject_names_containing in name]
        rej_ids = {tid for tid, _ in rejected}
        order = [tid for tid, _ in cands]
        chosen = [tid for tid in order if tid not in rej_ids][:slots]
        if s.defer_prioritize > 0:       # "nothing can start yet": the candidates stay Pending
            s.defer_prioritize -= 1
            chosen = []
        return self._json(request, {
            "order": order, "next_task_ids": chosen,
            "next_task_id": chosen[0] if chosen else None,
            "rejections": [{"task_id": tid, "reason": "Does not serve the Mission"}
                           for tid, _ in rejected]})

    def _answer_classify(self, request, messages, user, fresh, rec):
        return self._json(request, {"complexity": self.scenario.complexity})

    # -- work (tool loop) -----------------------------------------------------------

    def _answer_work(self, request, messages, user, fresh, rec):
        s = self.scenario
        written: set[str] = set()
        for m in messages:
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name") == "write_file":
                    try:
                        written.add(json.loads(fn.get("arguments") or "{}").get("path", ""))
                    except ValueError:
                        pass
        tid = _TASK_RE.search(user)
        task_id = tid.group(1) if tid else "task"
        needs: dict[str, dict] = {}
        for c in parse_criteria(user):
            n = needs.setdefault(c["path"], {"contains": [], "min_words": 0, "json": False})
            if c["kind"] == "min_words":
                try:
                    n["min_words"] = max(n["min_words"], int(float(c["arg"] or 0)))
                except ValueError:
                    pass
            elif c["kind"] == "file_contains" and c["arg"]:
                n["contains"].append(c["arg"])
            elif c["kind"] == "json_valid":
                n["json"] = True
        art = re.search(r"^Artifact to create or change: (.+)$", user, re.MULTILINE)
        if art and art.group(1).strip() not in needs:
            needs[art.group(1).strip()] = {"contains": [], "min_words": 0, "json": False}
        if not needs:
            needs[f"deliverable-{task_id[:8]}.md"] = {"contains": [], "min_words": 0,
                                                       "json": False}
        todo = [p for p in needs if p not in written]
        if todo:
            calls: list[tuple[str, dict]] = [
                ("write_file", {"path": p, "content": build_file_content(p, needs[p], p)})
                for p in todo]
            if not written:  # first reply only: optional extra tools
                if s.work_remember:
                    calls.append(("remember", {"content": "The fake agent remembers this task."}))
                if s.work_propose_team and not self._proposed_once:
                    self._proposed_once = True
                    calls.append(("propose_team", {
                        "name": s.work_propose_team,
                        "mission_statement": f"To advance the Vision, {s.work_propose_team} "
                                             f"owns a distinct part of it.",
                        "reason": "A separate team would do this better."}))
            return self._tool_calls(request, calls)
        if not s.work_submit:
            return self._text(request, "I have finished the work.")
        return self._tool_calls(request, [("submit", {
            "summary": f"Wrote {len(needs)} file(s) ({', '.join(needs)}); every acceptance "
                       f"criterion is met."})])

    # -- judges ---------------------------------------------------------------------

    def _review_attempt(self, user: str, judge: str, fresh: bool) -> int:
        tid = _TASK_RE.search(user)
        key = (tid.group(1) if tid else "?", judge)
        if fresh:
            self._review_attempts[key] = self._review_attempts.get(key, 0) + 1
        return self._review_attempts.get(key, 1)

    def _blocks(self, judge: str, attempt: int) -> bool:
        s = self.scenario
        if s.review_fail_judge not in (judge, "both"):
            return False
        return attempt <= s.review_fail_attempts

    def _answer_review_quality(self, request, messages, user, fresh, rec):
        attempt = self._review_attempt(user, "quality", fresh)
        rubric = [int(m.group(1)) for m in re.finditer(
            r"^(\d+)\. \[RUBRIC - you score it\]", user, re.MULTILINE)]
        red = re.findall(r"^\d+\. \[[a-z_]+: automatically checked, result RED.*?\] (.*)$",
                         user, re.MULTILINE)
        if red:
            return self._json(request, {
                "passed": False, "quality_score": 0.5,
                "criteria": [{"ordinal": o, "passed": False, "score": 0.5,
                              "reason": "An automatic check is red, so this cannot be certain."}
                             for o in rubric],
                "feedback": "Fix the failing automatic checks: " + "; ".join(red)})
        if self._blocks("quality", attempt):
            return self._json(request, {
                "passed": False, "quality_score": 0.6,
                "criteria": [{"ordinal": o, "passed": False, "score": 0.6,
                              "reason": "The conclusion paragraph is missing."} for o in rubric],
                "feedback": "Add a short concluding paragraph that restates the Overview."})
        return self._json(request, {
            "passed": True, "quality_score": 1.0,
            "criteria": [{"ordinal": o, "passed": True, "score": 1.0,
                          "reason": "The file states this clearly; no defect found."}
                         for o in rubric],
            "feedback": ""})

    def _answer_review_mission(self, request, messages, user, fresh, rec):
        s = self.scenario
        attempt = self._review_attempt(user, "mission", fresh)
        if fresh and s.empty_feedback_first > 0:
            s.empty_feedback_first -= 1
            return self._json(request, {"expected_value_met": False, "confidence": 0.5,
                                        "reasoning": "Not sure.", "feedback": ""})
        if self._blocks("mission", attempt):
            return self._json(request, {
                "expected_value_met": False, "confidence": 0.4,
                "reasoning": "The work does not yet cover the stakeholder.",
                "feedback": "Add a paragraph that names the stakeholder and what they get."})
        return self._json(request, {"expected_value_met": True, "confidence": 0.995,
                                    "reasoning": "The work advances the Mission.",
                                    "feedback": ""})

    def _answer_vote(self, request, messages, user, fresh, rec):
        ok = self.scenario.vote_approve
        return self._json(request, {"approve": ok, "reason": (
            "A separate team fits this part of the Vision." if ok
            else "Our Mission already covers this.")})

    def _answer_assess(self, request, messages, user, fresh, rec):
        s = self.scenario
        m = re.search(r"^Team: (.+)$", user, re.MULTILINE)
        team = m.group(1).strip() if m else "team"
        if fresh and s.empty_feedback_first > 0:
            s.empty_feedback_first -= 1
            return self._json(request, {"status": "continue", "confidence": 0.6,
                                        "reasoning": "More polish may help.", "next_focus": ""})
        if fresh and s.assess_restart_count > 0:
            s.assess_restart_count -= 1
            return self._json(request, {
                "status": "restart", "confidence": 0.6,
                "reasoning": "The approach is not working.",
                "next_focus": "Take a different approach: start from the Overview."})
        if self._plan_calls.get(team, 0) < s.plan_rounds:
            return self._json(request, {
                "status": "continue", "confidence": 0.8,
                "reasoning": "Part of the Mission is still missing.",
                "next_focus": "Cover the next part of the Mission."})
        conf = s.accomplished_confidence
        return self._json(request, {
            "status": "accomplished", "confidence": conf,
            "reasoning": "The done work covers the Mission.",
            "next_focus": "" if conf >= 0.99 else "Add the missing detail on the Overview."})

    def _answer_retrospective(self, request, messages, user, fresh, rec):
        return self._json(request, {"learnings": [
            {"kind": "pattern", "content": "Give every task a file_exists check."},
            {"kind": "routing", "content": "Simple documents are fine on the fast model."}]})
