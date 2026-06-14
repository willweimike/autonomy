from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecipeStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"


class TerminationReason(str, Enum):
    ACHIEVED = "achieved"
    BLOCKED = "blocked"
    NO_CANDIDATES = "no_candidates"
    APPROVAL_DENIED = "approval_denied"
    MAX_STEPS_REACHED = "max_steps_reached"
    FAILED = "failed"


class GoalStatus(str, Enum):
    CONTINUE = "continue"
    ACHIEVED = "achieved"
    BLOCKED = "blocked"


class ConversationMode(str, Enum):
    CHAT = "chat"
    TASK = "task"


class LearningProposalType(str, Enum):
    NEW_SKILL = "new_skill"
    PATCH_SKILL = "patch_skill"
    MERGE_SKILLS = "merge_skills"
    NO_LEARNING = "no_learning"


class LearningProposalStatus(str, Enum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"


@dataclass(frozen=True)
class Goal:
    text: str


@dataclass(frozen=True)
class ActionIntent:
    tool: str
    arguments: dict[str, Any]
    purpose: str = ""
    recipe_id: str | None = None
    evidence_strength: float = 0.0

    @property
    def fingerprint(self) -> str:
        value = json.dumps(
            {"tool": self.tool, "arguments": self.arguments},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Action:
    tool: str
    arguments: dict[str, Any]
    expected_effect: str
    verification_plan: str
    purpose: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    verifiable: bool = True
    safety_allowed: bool = True
    permission_allowed: bool = True
    goal_progress: float = 0.0
    evidence_strength: float = 0.0
    cost: float = 0.0
    uncertainty: float = 0.0
    recipe_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def fingerprint(self) -> str:
        value = json.dumps(
            {"tool": self.tool, "arguments": self.arguments},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class CandidatePath:
    actions: list[ActionIntent]
    source: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    score: float = 0.0
    score_details: dict[str, float] = field(default_factory=dict)
    penalty_reasons: list[str] = field(default_factory=list)
    rejection_reason: str = ""

    @property
    def next_action(self) -> ActionIntent:
        if not self.actions:
            raise ValueError("candidate path has no actions")
        return self.actions[0]


@dataclass(frozen=True)
class Observation:
    action_id: str
    succeeded: bool
    output: str = ""
    error: str = ""
    evidence: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    exit_code: int | None = None


@dataclass(frozen=True)
class Outcome:
    execution_ok: bool
    goal_status: GoalStatus
    reason: str
    evidence: tuple[str, ...] = ()
    confidence: float = 1.0


@dataclass(frozen=True)
class Transition:
    run_id: str
    step: int
    action: Action
    observation: Observation
    outcome: Outcome


@dataclass
class RunState:
    run_id: str
    goal: Goal
    step: int = 0
    transitions: list[Transition] = field(default_factory=list)
    current_state: str = "No actions have been executed."
    conversation_context: str = ""


@dataclass(frozen=True)
class RunResult:
    run_id: str
    goal: str
    termination: TerminationReason
    steps_executed: int
    reason: str


@dataclass(frozen=True)
class ConversationDecision:
    mode: ConversationMode
    task_goal: str = ""
    reason: str = ""


@dataclass(frozen=True)
class ConversationTurn:
    id: str
    session_id: str
    role: str
    content: str
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True)
class ConversationResponse:
    session_id: str
    user_turn_id: str
    assistant_turn_id: str
    run_result: RunResult | None
    reply: str
    conversation_context: str = ""
    candidate_skills: tuple[dict[str, Any], ...] = ()
    decision: ConversationDecision | None = None


@dataclass(frozen=True)
class ActionRecipe:
    id: str
    intent: str
    preconditions: str
    action_template: dict[str, Any]
    expected_effect: str
    verification_plan: str
    status: RecipeStatus = RecipeStatus.CANDIDATE
    enabled: bool = True
    evidence_count: int = 0


@dataclass(frozen=True)
class ProcedureSkillSummary:
    name: str
    description: str
    version: str
    tags: tuple[str, ...]
    platforms: tuple[str, ...]
    requires_tools: tuple[str, ...]
    source: str
    path: str
    file_hash: str
    enabled: bool = True


@dataclass(frozen=True)
class ProcedureSkill:
    summary: ProcedureSkillSummary
    body: str
    raw_content: str


@dataclass(frozen=True)
class ProcedureSkillDraft:
    name: str
    description: str
    body: str
    tags: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ("macos", "linux", "windows")
    requires_tools: tuple[str, ...] = ()
    version: str = "0.1.0"


@dataclass(frozen=True)
class LearningProposal:
    id: str
    proposal_type: LearningProposalType
    source_run_id: str
    reason: str
    confidence: float
    payload: dict[str, Any]
    status: LearningProposalStatus = LearningProposalStatus.CANDIDATE


def jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value
