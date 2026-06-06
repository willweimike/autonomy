from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from candidate_selection.models import CandidatePath, CandidateStep


class ActivationState(str, Enum):
    ACTIVATED = "activated"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class AutonomyState:
    goal: str
    current_state: str
    constraints: Dict[str, str] = field(default_factory=dict)
    completed: bool = False


@dataclass(frozen=True)
class ActionReadinessDecision:
    allowed: bool
    reason: str = ""


@dataclass
class ActivatedCandidate:
    candidate: CandidatePath
    activated_step: CandidateStep
    state: ActivationState = ActivationState.ACTIVATED


@dataclass(frozen=True)
class ExecutionResult:
    succeeded: bool
    externally_verified: bool
    reason: str
    evidence: str = ""
    proposed_edge_ids: List[str] = field(default_factory=list)
