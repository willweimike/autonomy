from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable

from .models import ActionIntent, CandidatePath, RiskLevel


class CandidateSelector:
    """Rank candidate paths with penalties, leaving execution checks to ActionGateway."""

    WEIGHTS = {
        "purpose": 0.10,
        "evidence_strength": 0.30,
        "risk": -0.35,
        "side_effects": -0.20,
        "penalty": -1.0,
    }
    RISK_SCORE = {RiskLevel.LOW: 0.0, RiskLevel.MEDIUM: 0.5, RiskLevel.HIGH: 1.0}
    PENALTIES = {
        "candidate has no actions": 1000.0,
        "action already succeeded with accepted outcome in this run": 0.75,
        "tool is unavailable": 1.0,
        "invalid tool arguments": 1.0,
    }

    def __init__(self, beam_width: int = 3):
        if beam_width < 1:
            raise ValueError("beam_width must be at least 1")
        self.beam_width = beam_width

    def select(
        self,
        candidates: Iterable[CandidatePath],
        available_tools: set[str],
        blocked_action_fingerprints: set[str] | None = None,
        action_rejection_reason: Callable[[ActionIntent], str] | None = None,
        action_risk: Callable[[ActionIntent], RiskLevel] | None = None,
        action_side_effects: Callable[[ActionIntent], tuple[str, ...]] | None = None,
    ) -> list[CandidatePath]:
        blocked_action_fingerprints = blocked_action_fingerprints or set()
        unique: dict[tuple[str, ...], CandidatePath] = {}
        for candidate in candidates:
            candidate.penalty_reasons = self.penalty_reasons(
                candidate,
                available_tools,
                blocked_action_fingerprints,
                action_rejection_reason,
            )
            key = tuple(action.fingerprint for action in candidate.actions)
            unique.setdefault(key, candidate)

        ranked = [
            self._score(
                candidate,
                action_risk=action_risk,
                action_side_effects=action_side_effects,
            )
            for candidate in unique.values()
        ]
        return sorted(ranked, key=lambda item: item.score, reverse=True)[: self.beam_width]

    @staticmethod
    def penalty_reasons(
        candidate: CandidatePath,
        available_tools: set[str],
        blocked_action_fingerprints: set[str] | None = None,
        action_rejection_reason: Callable[[ActionIntent], str] | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        if not candidate.actions:
            return ["candidate has no actions"]
        if candidate.next_action.fingerprint in (blocked_action_fingerprints or set()):
            reasons.append("action already succeeded with accepted outcome in this run")
        for action in candidate.actions:
            if action.tool not in available_tools:
                reasons.append(f"tool is unavailable: {action.tool}")
            if action_rejection_reason:
                reason = action_rejection_reason(action)
                if reason:
                    reasons.append(reason)
        return reasons

    def _score(
        self,
        candidate: CandidatePath,
        *,
        action_risk: Callable[[ActionIntent], RiskLevel] | None = None,
        action_side_effects: Callable[[ActionIntent], tuple[str, ...]] | None = None,
    ) -> CandidatePath:
        if not candidate.actions:
            candidate.score_details = {"penalty": self.PENALTIES["candidate has no actions"]}
            candidate.score = float("-inf")
            return candidate
        count = float(len(candidate.actions))
        penalty = sum(self._penalty_value(reason) for reason in candidate.penalty_reasons)
        risk = action_risk or (lambda action: RiskLevel.LOW)
        side_effects = action_side_effects or (lambda action: ())
        details = {
            "purpose": sum(1.0 if a.purpose.strip() else 0.0 for a in candidate.actions) / count,
            "evidence_strength": sum(a.evidence_strength for a in candidate.actions) / count,
            "risk": sum(self.RISK_SCORE[risk(a)] for a in candidate.actions) / count,
            "side_effects": sum(1.0 if side_effects(a) else 0.0 for a in candidate.actions) / count,
            "penalty": penalty,
        }
        candidate.score_details = details
        candidate.score = sum(details[name] * self.WEIGHTS[name] for name in details)
        return candidate

    @classmethod
    def _penalty_value(cls, reason: str) -> float:
        for prefix, value in cls.PENALTIES.items():
            if reason == prefix or reason.startswith(f"{prefix}:"):
                return value
        return 1.0
