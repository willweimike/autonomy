from __future__ import annotations

from typing import Dict

from .models import CandidatePath


class UtilityScorer:
    """Multi-objective utility scoring biased toward verifiable, low-risk paths."""

    DEFAULT_WEIGHTS = {
        "goal_progress": 0.30,
        "verifiability": 0.25,
        "edge_confidence": 0.20,
        "evidence_strength": 0.15,
        "skill_availability": 0.10,
        "risk": -0.35,
        "cost": -0.20,
        "uncertainty": -0.20,
        "penalty": -1.0,
    }
    PENALTIES = {
        "candidate has no steps": 1000.0,
        "safety not allowed": 0.5,
        "permission not allowed": 0.5,
        "candidate is not externally verifiable": 0.4,
    }

    def __init__(self, weights: Dict[str, float] = None):
        self.weights = dict(self.DEFAULT_WEIGHTS)
        if weights:
            self.weights.update(weights)

    def score(self, candidate: CandidatePath) -> CandidatePath:
        if not candidate.steps:
            candidate.score = float("-inf")
            candidate.score_details = {"empty_path": 1.0}
            return candidate

        aggregates = {
            "goal_progress": 0.0,
            "verifiability": 0.0,
            "edge_confidence": 0.0,
            "evidence_strength": 0.0,
            "skill_availability": 0.0,
            "risk": 0.0,
            "cost": 0.0,
            "uncertainty": 0.0,
            "penalty": 0.0,
        }
        count = float(len(candidate.steps))
        aggregates["penalty"] = sum(
            self._penalty_value(reason) for reason in candidate.penalty_reasons
        )
        for step in candidate.steps:
            aggregates["goal_progress"] += step.goal_progress
            aggregates["verifiability"] += step.verifiability
            aggregates["edge_confidence"] += step.edge_confidence
            aggregates["evidence_strength"] += step.evidence_strength
            aggregates["skill_availability"] += step.skill_availability
            aggregates["risk"] += step.risk
            aggregates["cost"] += step.cost
            aggregates["uncertainty"] += step.uncertainty

        details = {
            key: (value if key == "penalty" else value / count)
            for key, value in aggregates.items()
        }
        candidate.score_details = details
        candidate.score = sum(details[key] * self.weights[key] for key in details)
        return candidate

    @classmethod
    def _penalty_value(cls, reason: str) -> float:
        for prefix, value in cls.PENALTIES.items():
            if reason == prefix or reason.startswith(f"{prefix}:"):
                return value
        return 1.0
