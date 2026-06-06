from __future__ import annotations

from typing import Callable, Iterable, List, Protocol

from candidate_selection import BeamSearchSelector, CandidatePath, CandidateStatus

from .models import (
    ActionReadinessDecision,
    ActivatedCandidate,
    ActivationState,
    AutonomyState,
    ExecutionResult,
)


class CandidateProvider(Protocol):
    def generate(self, state: AutonomyState) -> Iterable[CandidatePath]:
        ...


class AutonomyCore:
    """Control layer that turns candidate paths into one bounded activation."""

    def __init__(
        self,
        providers: Iterable[CandidateProvider],
        selector: BeamSearchSelector = None,
        readiness_check: Callable[[AutonomyState, CandidatePath], ActionReadinessDecision] = None,
    ):
        self.providers = list(providers)
        self.selector = selector or BeamSearchSelector(beam_width=3)
        self.readiness_check = readiness_check or self._default_readiness_check

    def candidates_for(self, state: AutonomyState) -> List[CandidatePath]:
        candidates: List[CandidatePath] = []
        for provider in self.providers:
            candidates.extend(list(provider.generate(state)))
        return candidates

    def rank_candidates(self, state: AutonomyState) -> List[CandidatePath]:
        if state.completed:
            return []
        return self.selector.select(self.candidates_for(state))

    def activate_next(self, state: AutonomyState) -> ActivatedCandidate:
        ranked = self.rank_candidates(state)
        for candidate in ranked:
            decision = self.readiness_check(state, candidate)
            if decision.allowed:
                candidate.status = CandidateStatus.ACTIVATED
                return ActivatedCandidate(
                    candidate=candidate,
                    activated_step=candidate.next_step,
                    state=ActivationState.ACTIVATED,
                )
            candidate.rejection_reason = decision.reason
        raise RuntimeError("No candidate passed action readiness checks")

    def close_activation(
        self,
        activation: ActivatedCandidate,
        result: ExecutionResult,
    ) -> ActivatedCandidate:
        if result.succeeded and result.externally_verified:
            activation.state = ActivationState.COMPLETED
            activation.candidate.status = CandidateStatus.COMPLETED
        elif result.succeeded and not result.externally_verified:
            activation.state = ActivationState.PAUSED
            activation.candidate.status = CandidateStatus.PAUSED
        else:
            activation.state = ActivationState.FAILED
            activation.candidate.status = CandidateStatus.FAILED
        return activation

    @staticmethod
    def _default_readiness_check(
        state: AutonomyState,
        candidate: CandidatePath,
    ) -> ActionReadinessDecision:
        if not candidate.steps:
            return ActionReadinessDecision(False, "candidate has no steps")
        step = candidate.next_step
        if not step.safety_allowed:
            return ActionReadinessDecision(False, "safety not allowed")
        if not step.permission_allowed:
            return ActionReadinessDecision(False, "permission not allowed")
        if not step.verifiable:
            return ActionReadinessDecision(False, "next step is not verifiable")
        return ActionReadinessDecision(True)
