from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class EdgeType(str, Enum):
    PRECEDES = "precedes"
    ENABLES = "enables"
    VERIFIES = "verifies"
    REMEDIATES = "remediates"
    ALTERNATIVE_TO = "alternative_to"


class EdgeStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class SituationSkillNode:
    """A graph node tying a skill to the situation where it was useful."""

    node_id: str
    situation: str
    skill_name: str
    condition: str
    evidence: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class SkillGraphEdge:
    """Typed, evidence-backed relationship between two situation-skill nodes."""

    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: EdgeType
    condition: str
    evidence: str = ""
    alpha: int = 1
    beta: int = 1
    status: EdgeStatus = EdgeStatus.PROPOSED
    last_verified_at: Optional[str] = None
    failure_conditions: List[str] = field(default_factory=list)

    @property
    def confidence_mean(self) -> float:
        return self.alpha / float(self.alpha + self.beta)

    @property
    def evidence_count(self) -> int:
        return max(0, self.alpha + self.beta - 2)

    def record_success(self, verified_at: Optional[str] = None) -> None:
        self.alpha += 1
        self.last_verified_at = verified_at

    def record_failure(
        self,
        failure_condition: Optional[str] = None,
        verified_at: Optional[str] = None,
    ) -> None:
        self.beta += 1
        self.last_verified_at = verified_at
        if failure_condition:
            self.failure_conditions.append(failure_condition)
