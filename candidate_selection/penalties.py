from __future__ import annotations

from typing import Iterable, List

from .models import CandidatePath


class CandidatePenaltyAnnotator:
    """Annotate candidate issues as scoring penalties without filtering them out."""

    def annotate(self, candidates: Iterable[CandidatePath]) -> List[CandidatePath]:
        accepted = []
        for candidate in candidates:
            candidate.penalty_reasons = self.penalty_reasons(candidate)
            accepted.append(candidate)
        return accepted

    def penalty_reasons(self, candidate: CandidatePath) -> List[str]:
        reasons: List[str] = []
        if not candidate.steps:
            return ["candidate has no steps"]
        for step in candidate.steps:
            if not step.safety_allowed:
                reasons.append("safety not allowed")
            if not step.permission_allowed:
                reasons.append("permission not allowed")
            if not step.verifiable:
                reasons.append("candidate is not externally verifiable")
        return reasons
