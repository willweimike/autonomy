from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List


class CandidateStatus(str, Enum):
    GENERATED = "generated"
    RANKED = "ranked"
    SELECTED = "selected"
    ACTIVATED = "activated"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


class CandidateSource(str, Enum):
    SKILL_GRAPH = "skill_graph"
    MODEL_PROPOSED = "model_proposed"
    POLICY_DEFAULT = "policy_default"


@dataclass(frozen=True)
class CandidateStep:
    skill_name: str
    action: str
    situation: str = ""
    verifiable: bool = True
    safety_allowed: bool = True
    permission_allowed: bool = True
    goal_progress: float = 0.0
    verifiability: float = 1.0
    edge_confidence: float = 0.5
    evidence_strength: float = 0.0
    skill_availability: float = 1.0
    risk: float = 0.0
    cost: float = 0.0
    uncertainty: float = 0.0


@dataclass
class CandidatePath:
    path_id: str
    steps: List[CandidateStep]
    source: CandidateSource
    status: CandidateStatus = CandidateStatus.GENERATED
    score: float = 0.0
    score_details: Dict[str, float] = field(default_factory=dict)
    penalty_reasons: List[str] = field(default_factory=list)
    rejection_reason: str = ""

    @property
    def next_step(self) -> CandidateStep:
        if not self.steps:
            raise ValueError("CandidatePath has no steps")
        return self.steps[0]
