"""DAASH 0.1 domain model — the shared contract for repo, policy, engine and API.

See docs/DESIGN-0.1.md for how these types fit together. Everything here is
plain data (pydantic); no I/O. Enum values are what is stored in PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class SwarmStatus(str, Enum):
    """Swarm lifecycle (see 'Swarm State Diagram.mermaid')."""
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    USER_FEEDBACK = "USER_FEEDBACK"
    VERIFICATION = "VERIFICATION"
    LEARNING = "LEARNING"
    ARCHIVED = "ARCHIVED"
    DELETED = "DELETED"

    @staticmethod
    def terminal() -> tuple[str, ...]:
        return ("ARCHIVED", "DELETED")


#: Lifecycle action verb -> {from_state: to_state}. Pure data; policy.transition
#: applies it. Mirrors the Mermaid diagram (FINAL_VERIFY=verify, IGNORE=archive).
SWARM_ACTIONS: dict[str, dict[str, str]] = {
    "start":    {"CREATED": "ACTIVE"},
    "pause":    {"ACTIVE": "PAUSED", "USER_FEEDBACK": "PAUSED"},
    "resume":   {"PAUSED": "ACTIVE", "USER_FEEDBACK": "ACTIVE"},
    "feedback": {"PAUSED": "USER_FEEDBACK"},
    "verify":   {"ACTIVE": "VERIFICATION", "PAUSED": "VERIFICATION"},
    "learn":    {"VERIFICATION": "LEARNING"},
    "archive":  {"VERIFICATION": "ARCHIVED", "LEARNING": "ARCHIVED"},
    "delete":   {"CREATED": "DELETED", "VERIFICATION": "DELETED"},
}


class TeamStatus(str, Enum):
    ACTIVE = "active"              # working its mission
    ACCOMPLISHED = "accomplished"  # Mission Judgement confirmed the mission is met
    FAILED = "failed"              # gave up (e.g. task budget exhausted)


class CreatorKind(str, Enum):
    USER = "user"                            # supplied when the swarm was created
    SWARM_COORDINATOR = "swarm_coordinator"  # bootstrapped from the Vision
    TEAM = "team"                            # spun out by another team's majority vote


class AgentRole(str, Enum):
    SWARM_COORDINATOR = "swarm_coordinator"
    MISSION_JUDGEMENT = "mission_judgement"
    QUALITY_JUDGEMENT = "quality_judgement"
    MISSION_DELIVERY = "mission_delivery"


class AgentState(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    STOPPED = "stopped"


class TaskStatus(str, Enum):
    PENDING = "Pending"          # backlog; not yet approved by Mission Judgement
    READY = "Ready"              # approved + prioritised by Mission Judgement
    IN_PROGRESS = "In-Progress"  # committed (DoR passed); a Delivery agent owns it
    REVIEW = "Review"            # submitted; awaiting Quality + Mission verdicts
    DONE = "Done"                # passed both levels of the Definition of Done
    FAILED = "Failed"            # exhausted max attempts
    REJECTED = "Rejected"        # Mission Judgement rejected it (misaligned/obsolete)

    @staticmethod
    def open_statuses() -> tuple[str, ...]:
        return ("Pending", "Ready", "In-Progress", "Review")

    @staticmethod
    def terminal_statuses() -> tuple[str, ...]:
        return ("Done", "Failed", "Rejected")


class CriterionStatus(str, Enum):
    UNKNOWN = "unknown"
    GREEN = "green"
    RED = "red"


class CheckKind(str, Enum):
    """How an acceptance criterion is verified.

    Everything except RUBRIC is deterministic and evaluated by code
    (app/checks.py) against the swarm workspace; RUBRIC is judged by the
    Quality Judgement agent with a 0..1 score.
    """
    RUBRIC = "rubric"                # check_path optional (file to judge)
    FILE_EXISTS = "file_exists"      # check_path required
    FILE_CONTAINS = "file_contains"  # check_path + check_arg (regex, case-insensitive)
    MIN_WORDS = "min_words"          # check_path + check_arg (integer)
    JSON_VALID = "json_valid"        # check_path required


DETERMINISTIC_CHECKS: frozenset[str] = frozenset(
    {CheckKind.FILE_EXISTS.value, CheckKind.FILE_CONTAINS.value,
     CheckKind.MIN_WORDS.value, CheckKind.JSON_VALID.value})


class Complexity(str, Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"


class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class LearningKind(str, Enum):
    PATTERN = "pattern"        # learning mechanism 1: patterns improving value/quality/speed
    AUTOMATION = "automation"  # mechanism 2: deterministic scripts (recorded, not yet executed)
    ROUTING = "routing"        # mechanism 3: allocating work to the cheapest capable model


#: Vision reviews a swarm may use before it completes regardless (docs/DESIGN-0.3.md section 10).
MAX_VISION_REVIEWS = 3


class ActionKind(str, Enum):
    """What the engine's pure decide() step can ask the executor to do."""
    PAUSE_FOR_LIMITS = "pause_for_limits"      # deterministic
    BOOTSTRAP = "bootstrap"                    # coordinator: vision DoD and/or initial teams
    MATERIALIZE_TEAM = "materialize_team"      # deterministic: approved proposal -> team
    GENERATE_STANDARDS = "generate_standards"  # quality judgement: team DoR/DoD
    PLAN_TASKS = "plan_tasks"                  # delivery: create tasks with acceptance criteria
    PRIORITIZE = "prioritize"                  # mission judgement: approve next task / reject
    COMMIT_TASK = "commit_task"                # deterministic: DoR gate passed -> In-Progress
    WORK_TASK = "work_task"                    # delivery: tool loop, ends in submit
    REVIEW_TASK = "review_task"                # checks + quality + mission verdicts -> DoD
    ASSESS_MISSION = "assess_mission"          # mission judgement: continue/accomplished/restart
    FAIL_TEAM = "fail_team"                    # deterministic: team exhausted its task budget
    COMPLETE_SWARM = "complete_swarm"          # deterministic: all teams terminal -> VERIFICATION
    RETROSPECTIVE = "retrospective"            # coordinator in LEARNING: swarm-wide learnings
    VOTE = "vote"                              # delivery agents vote on a pending team proposal
    EXPIRE_PROPOSAL = "expire_proposal"        # deterministic: stale/orphaned proposal -> rejected
    REVIEW_VISION = "review_vision"            # coordinator: all teams done -> vision-level DoD review


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Gateway model aliases ordered cheapest -> most capable. The m3u512gb gateway
#: serves exactly these; other providers can override via SwarmConfig.
DEFAULT_MODEL_LADDER: list[str] = ["instant", "fast", "great", "amazing"]


#: Harness names (docs/DESIGN-0.3.md section 1): "daash" is the native tool loop, the rest run
#: on external agent CLIs through a pull-based runner ("fake" is the scripted test CLI).
NATIVE_HARNESS = "daash"
KNOWN_HARNESSES: tuple[str, ...] = ("daash", "openclaude", "claude-code", "codex", "fake")


#: What an agent can do (docs/DESIGN-0.3.md section 11). `edit_files` is implied for every task.
class Capability(str, Enum):
    EDIT_FILES = "edit_files"      # create / modify workspace files
    RUN_CODE = "run_code"          # execute shell commands, scripts and tests
    WEB_RESEARCH = "web_research"  # search / fetch the web


CAPABILITY_NAMES: tuple[str, ...] = tuple(c.value for c in Capability)

#: Default capabilities per harness; HarnessSpec.capabilities overrides one delivery slot.
HARNESS_CAPABILITIES: dict[str, list[str]] = {
    "daash": ["edit_files"],
    "openclaude": ["edit_files", "run_code"],
    "claude-code": ["edit_files", "run_code"],
    "codex": ["edit_files", "run_code"],
    "fake": ["edit_files", "run_code"],
}


def normalize_capabilities(raw: Any) -> list[str]:
    """Known capability names only, de-duplicated, in vocabulary order; anything else dropped."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    have = {str(x).strip().lower() for x in raw}
    return [c for c in CAPABILITY_NAMES if c in have]


def harness_capabilities(harness: str, override: Optional[list[str]] = None) -> list[str]:
    if override is not None:
        return normalize_capabilities(override)
    return list(HARNESS_CAPABILITIES.get(harness, ["edit_files"]))


def required_capabilities(raw: Any) -> list[str]:
    """Capabilities a task needs: known values only; `edit_files` is implied, so not stored."""
    return [c for c in normalize_capabilities(raw) if c != Capability.EDIT_FILES.value]


class HarnessSpec(BaseModel):
    harness: str = "daash"      # "daash" = native OpenAI-compatible tool loop (see KNOWN_HARNESSES)
    provider: str = "default"   # the endpoint configured by DAASH_LLM_ENDPOINT
    model: str = "amazing"      # "" = use routing (the ladder) for delivery_harnesses entries
    capabilities: Optional[list[str]] = None   # None = the harness default (HARNESS_CAPABILITIES)


class RoleModels(BaseModel):
    """Base model per role. Delivery work may escalate up the ladder on failure."""
    mission_judgement: str = "great"
    quality_judgement: str = "great"
    mission_delivery: str = "fast"
    classifier: str = "instant"   # cheap task-complexity classifier (learning mechanism 3)


class Limits(BaseModel):
    max_wall_clock_minutes: Optional[int] = None   # since first start; None = unlimited
    max_total_tokens: Optional[int] = 5_000_000    # all agent calls in the swarm
    max_budget_usd: Optional[float] = None         # priced via SwarmConfig.model_prices
    max_simultaneous_agents: int = Field(3, ge=1)  # concurrent LLM jobs
    max_teams: int = Field(4, ge=1)
    max_tasks_per_team: int = Field(25, ge=1)      # lifetime; tasks rejected before start are free (up to this many)
    max_new_tasks_per_plan: int = Field(4, ge=1)
    max_attempts_per_task: int = Field(3, ge=1)
    max_tool_steps: int = Field(24, ge=1)          # per work attempt
    remote_job_timeout_minutes: int = Field(30, ge=1)   # wall time of one remote attempt (0.3)


class DoDThresholds(BaseModel):
    """README Definition of Done minimums."""
    deterministic: float = 1.0        # 100% of deterministic checks
    rubric: float = 0.999             # every rubric/statistical check > 99.9%
    mission_confidence: float = 0.99  # expected value met with >= 99% confidence


class SwarmConfig(BaseModel):
    allowed_harnesses: list[str] = Field(default_factory=lambda: ["daash"])
    allowed_providers: list[str] = Field(default_factory=lambda: ["default"])
    allowed_models: list[str] = Field(default_factory=lambda: list(DEFAULT_MODEL_LADDER))
    model_ladder: list[str] = Field(default_factory=lambda: list(DEFAULT_MODEL_LADDER))
    coordinator: HarnessSpec = Field(default_factory=HarnessSpec)
    role_models: RoleModels = Field(default_factory=RoleModels)
    delivery_agents_per_team: int = Field(1, ge=1, le=5)
    limits: Limits = Field(default_factory=Limits)
    dod_thresholds: DoDThresholds = Field(default_factory=DoDThresholds)
    #: USD per 1k total tokens by model; local models default to free.
    model_prices: dict[str, float] = Field(default_factory=dict)
    #: Harness of each delivery agent, cycled: Delivery-i gets delivery_harnesses[(i-1) % len].
    #: Empty = every delivery agent is native "daash" (0.2 behaviour).
    delivery_harnesses: list[HarnessSpec] = Field(default_factory=list)
    #: A remote job no runner leased after this many minutes runs natively instead. None = wait.
    remote_fallback_minutes: Optional[int] = 10


class GlobalConfig(BaseModel):
    """Global (cross-swarm) settings: README 'Values and Principles'."""
    values_principles: str = ""
    default_swarm_config: SwarmConfig = Field(default_factory=SwarmConfig)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

class Swarm(BaseModel):
    id: str
    name: str
    vision_statement: str
    #: Vision-level Definition of Done (README: DoD has a vision level and a
    #: team-mission level). None until the Swarm Coordinator generates it or
    #: the user supplies it.
    definition_of_done: Optional[str] = None
    lifecycle_state: str = SwarmStatus.CREATED.value
    config: SwarmConfig = Field(default_factory=SwarmConfig)
    paused_reason: Optional[str] = None
    retrospective_done: bool = False
    vision_reviews: int = 0         # Swarm Coordinator vision reviews used (MAX_VISION_REVIEWS)
    created_at: datetime
    started_at: Optional[datetime] = None


class Team(BaseModel):
    id: str
    swarm_id: str
    name: str
    mission_statement: str
    status: str = TeamStatus.ACTIVE.value
    creator_kind: str = CreatorKind.USER.value
    creator_team_id: Optional[str] = None
    definition_of_ready: Optional[str] = None   # team-specific additions (Quality Judgement)
    definition_of_done: Optional[str] = None    # team-mission-level DoD (Quality Judgement)
    generation: int = 0                         # increments on a Mission Judgement "restart"
    next_focus: Optional[str] = None            # latest Mission Judgement steer for planning
    last_assessed_at: Optional[datetime] = None
    created_at: datetime


class Agent(BaseModel):
    id: str
    swarm_id: str
    team_id: Optional[str] = None   # None for the Swarm Coordinator
    role: str
    name: str                       # e.g. "Delivery-1"
    harness: str = "daash"
    capabilities: list[str] = Field(default_factory=list)   # see Capability
    provider: str = "default"
    model: str                      # base model for this role
    state: str = AgentState.IDLE.value
    current_task_id: Optional[str] = None
    tokens_used: int = 0
    calls: int = 0
    errors: int = 0
    created_at: datetime
    last_active_at: Optional[datetime] = None


class Runner(BaseModel):
    """An external device that runs agent CLIs for remote harnesses (docs/DESIGN-0.3.md section 3).
    The token hash is never part of this model's API form."""
    id: str
    name: str
    token_hash: str = Field(default="", exclude=True)
    enabled: bool = True
    host: Optional[str] = None
    version: Optional[str] = None
    harnesses: list[dict[str, Any]] = Field(default_factory=list)
    max_jobs: int = 1
    last_seen_at: Optional[datetime] = None
    created_at: datetime


class RemoteJob(BaseModel):
    """One remote attempt at a task: queued for a runner, leased, then completed/cancelled/abandoned."""
    id: str
    swarm_id: str
    team_id: str
    task_id: str
    agent_id: str
    attempt: int
    harness: str
    model: str
    status: str = "queued"          # queued|leased|completed|cancelled|abandoned
    runner_id: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    result: Optional[dict[str, Any]] = None
    expiries: int = 0
    lease_expires_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    created_at: datetime
    leased_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class AcceptanceCriterion(BaseModel):
    id: str
    task_id: str
    ordinal: int
    text: str
    check_kind: str = CheckKind.RUBRIC.value
    check_path: Optional[str] = None
    check_arg: Optional[str] = None
    status: str = CriterionStatus.UNKNOWN.value
    score: Optional[float] = None
    evidence: Optional[str] = None
    updated_at: Optional[datetime] = None


class Task(BaseModel):
    id: str
    swarm_id: str
    team_id: str
    parent_id: Optional[str] = None
    name: str
    description: str = ""
    artifact: str = ""              # workspace path (or name) of the artifact this task changes
    status: str = TaskStatus.PENDING.value
    priority: Optional[int] = None  # lower = higher priority; set by Mission Judgement
    complexity: Optional[str] = None
    depends_on: list[str] = Field(default_factory=list)  # task ids that must be Done first
    required_capabilities: list[str] = Field(default_factory=list)  # beyond edit_files
    assigned_agent_id: Optional[str] = None
    attempts: int = 0               # completed work attempts (each review or failed loop)
    created_by: str = "user"        # agent id or "user"
    rejection_reason: Optional[str] = None
    feedback: Optional[str] = None  # latest judge feedback for the next attempt
    deliverable: dict[str, Any] = Field(default_factory=dict)  # {"summary": str, "files": [..]}
    created_at: datetime
    updated_at: datetime
    approved_at: Optional[datetime] = None
    committed_at: Optional[datetime] = None
    done_at: Optional[datetime] = None
    criteria: list[AcceptanceCriterion] = Field(default_factory=list)


class Verdict(BaseModel):
    id: str
    task_id: str
    attempt: int
    judge_role: str        # quality_judgement | mission_judgement
    agent_id: Optional[str] = None
    passed: bool
    score: float           # QJ: quality score; MJ: confidence the expected value is met
    reasoning: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TaskEvent(BaseModel):
    """Append-only, human-readable audit/metrics log (task_id may be None)."""
    id: int
    swarm_id: str
    team_id: Optional[str] = None
    task_id: Optional[str] = None
    kind: str              # created|approved|rejected|committed|submitted|verdict|rework|done|failed|error|team|swarm|assessment|proposal|feedback
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    actor: str = "engine"  # agent id, "user" or "engine"
    detail: str = ""
    created_at: datetime


class AgentCall(BaseModel):
    """One LLM job (possibly many chat completions); token accounting + transcript."""
    id: str
    swarm_id: str
    team_id: Optional[str] = None
    agent_id: Optional[str] = None
    task_id: Optional[str] = None
    role: str
    model: str
    purpose: str           # the ActionKind value, e.g. "work_task"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    ok: bool = True
    error: Optional[str] = None
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class Learning(BaseModel):
    id: str
    swarm_id: Optional[str] = None   # None = global (carried into future swarms)
    team_id: Optional[str] = None
    task_id: Optional[str] = None
    kind: str = LearningKind.PATTERN.value
    content: str
    created_by: str = "engine"
    created_at: datetime


class Memory(BaseModel):
    """Persistent per-agent memory across sessions."""
    id: str
    agent_id: str
    swarm_id: str
    team_id: Optional[str] = None
    task_id: Optional[str] = None
    content: str
    created_at: datetime


class TeamProposal(BaseModel):
    """A Delivery agent's proposal to spin out a new team (majority vote)."""
    id: str
    swarm_id: str
    team_id: str                    # proposing team (becomes creator_team_id)
    proposer_agent_id: str
    name: str
    mission_statement: str
    reason: str = ""
    status: str = ProposalStatus.PENDING.value
    votes: dict[str, bool] = Field(default_factory=dict)  # agent_id -> approve
    quorum: int = 1
    created_team_id: Optional[str] = None
    created_at: datetime
    decided_at: Optional[datetime] = None


class Feedback(BaseModel):
    id: str
    swarm_id: str
    text: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Engine snapshot + actions (pure decide() works on these)
# ---------------------------------------------------------------------------

class SwarmSnapshot(BaseModel):
    swarm: Swarm
    teams: list[Team] = Field(default_factory=list)
    agents: list[Agent] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)        # all swarm tasks, with criteria
    proposals: list[TeamProposal] = Field(default_factory=list)
    p90_cycle_seconds: Optional[float] = None              # trailing, last 100 Done tasks
    tokens_used: int = 0
    cost_usd: float = 0.0
    now: datetime
    running_jobs: set[str] = Field(default_factory=set)    # job keys, see job_key()


class Action(BaseModel):
    kind: ActionKind
    team_id: Optional[str] = None
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    proposal_id: Optional[str] = None
    detail: str = ""

    def job_key(self) -> str:
        return job_key(self.kind, self.team_id, self.task_id)


def job_key(kind: ActionKind | str, team_id: Optional[str] = None,
            task_id: Optional[str] = None) -> str:
    k = kind.value if isinstance(kind, ActionKind) else kind
    return f"{k}:{team_id or '-'}:{task_id or '-'}"


# ---------------------------------------------------------------------------
# API request bodies
# ---------------------------------------------------------------------------

class TeamCreateItem(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    mission_statement: str = Field(..., min_length=1)


class SwarmCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    vision_statement: str = Field(..., min_length=1)
    definition_of_done: Optional[str] = None   # optional vision-level DoD
    config: Optional[SwarmConfig] = None       # defaults to GlobalConfig.default_swarm_config
    teams: list[TeamCreateItem] = Field(default_factory=list)  # empty -> coordinator bootstraps


class SwarmUpdate(BaseModel):
    name: Optional[str] = None
    vision_statement: Optional[str] = None     # only while CREATED
    definition_of_done: Optional[str] = None
    config: Optional[SwarmConfig] = None


class TeamCreate(BaseModel):
    swarm_id: str
    name: str = Field(..., min_length=1, max_length=255)
    mission_statement: str = Field(..., min_length=1)


class TeamUpdate(BaseModel):
    name: Optional[str] = None
    mission_statement: Optional[str] = None
    status: Optional[TeamStatus] = None


class CriterionCreate(BaseModel):
    text: str = Field(..., min_length=1)
    check_kind: CheckKind = CheckKind.RUBRIC
    check_path: Optional[str] = None
    check_arg: Optional[str] = None


class TaskCreate(BaseModel):
    team_id: str
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    artifact: str = ""
    parent_id: Optional[str] = None
    depends_on: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    acceptance_criteria: list[CriterionCreate] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    artifact: Optional[str] = None
    status: Optional[TaskStatus] = None
    priority: Optional[int] = None
    rejection_reason: Optional[str] = None


class FeedbackCreate(BaseModel):
    text: str = Field(..., min_length=1)


class QualityGenerateRequest(BaseModel):
    dor_guidance: str = ""
    dod_guidance: str = ""
