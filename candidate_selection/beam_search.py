from __future__ import annotations

from typing import Iterable, List

from .models import CandidatePath, CandidateStatus
from .penalties import CandidatePenaltyAnnotator
from .scoring import UtilityScorer


class BeamSearchSelector:
    """Keep the top-k scored candidate paths for Autonomy Core review."""

    def __init__(
        self,
        beam_width: int = 3,
        penalty_annotator: CandidatePenaltyAnnotator = None,
        scorer: UtilityScorer = None,
    ):
        if beam_width < 1:
            raise ValueError("beam_width must be at least 1")
        self.beam_width = beam_width
        self.penalty_annotator = penalty_annotator or CandidatePenaltyAnnotator()
        self.scorer = scorer or UtilityScorer()

    def select(self, candidates: Iterable[CandidatePath]) -> List[CandidatePath]:
        annotated = self.penalty_annotator.annotate(candidates)
        scored = [self.scorer.score(candidate) for candidate in annotated]
        ranked = sorted(scored, key=lambda item: item.score, reverse=True)
        selected = ranked[: self.beam_width]
        for candidate in selected:
            candidate.status = CandidateStatus.RANKED
        return selected
