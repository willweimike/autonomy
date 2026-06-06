from __future__ import annotations

import uuid

from .model import CandidateModel
from .models import (
    LearningProposal,
    LearningProposalStatus,
    LearningProposalType,
    RunState,
    TerminationReason,
)
from .procedure_skills import ProcedureSkillLibrary
from .store import AutonomyStore


class LearningLoop:
    """Post-run learning review that can propose, but not activate, skills."""

    def __init__(
        self,
        *,
        model: CandidateModel,
        store: AutonomyStore,
        procedure_skills: ProcedureSkillLibrary | None,
    ):
        self.model = model
        self.store = store
        self.procedure_skills = procedure_skills

    def review_run(
        self,
        state: RunState,
        *,
        termination: TerminationReason,
        reason: str,
    ) -> list[LearningProposal]:
        successful = [
            transition
            for transition in state.transitions
            if transition.observation.succeeded and transition.outcome.execution_ok
        ]
        if (
            termination == TerminationReason.ACHIEVED
            and len(successful) >= 2
            and self.procedure_skills
        ):
            return [self._create_new_skill_candidate(state, reason)]

        if termination == TerminationReason.BLOCKED and successful:
            return [
                self._record_no_learning(
                    state,
                    "blocked run has successful outcomes; patch proposals are not applied in v1",
                    confidence=0.5,
                )
            ]

        return [
            self._record_no_learning(
                state,
                "run did not meet learning threshold",
                confidence=1.0,
            )
        ]

    def _create_new_skill_candidate(self, state: RunState, reason: str) -> LearningProposal:
        draft = self.model.draft_procedure_skill(state)
        candidate = self.procedure_skills.write_candidate(
            draft,
            source_run_id=state.run_id,
            source_workspace=self.procedure_skills.workspace,
            proposal_type=LearningProposalType.NEW_SKILL.value,
            reason="achieved run with multiple successful outcomes",
            confidence=0.85,
        )
        proposal = LearningProposal(
            id=candidate["candidate_id"],
            proposal_type=LearningProposalType.NEW_SKILL,
            source_run_id=state.run_id,
            status=LearningProposalStatus.CANDIDATE,
            reason="achieved run with multiple successful outcomes",
            confidence=0.85,
            payload=candidate,
        )
        self.store.record_learning_proposal(proposal)
        self.store.record_event(
            state.run_id,
            state.step,
            "learning_review",
            {
                "proposal_id": proposal.id,
                "proposal_type": proposal.proposal_type.value,
                "status": proposal.status.value,
                "reason": proposal.reason,
                "run_reason": reason,
            },
        )
        self.store.record_event(
            state.run_id,
            state.step,
            "procedure_skill_candidate_created",
            candidate,
        )
        return proposal

    def _record_no_learning(
        self,
        state: RunState,
        reason: str,
        *,
        confidence: float,
    ) -> LearningProposal:
        proposal = LearningProposal(
            id=uuid.uuid4().hex,
            proposal_type=LearningProposalType.NO_LEARNING,
            source_run_id=state.run_id,
            status=LearningProposalStatus.APPLIED,
            reason=reason,
            confidence=confidence,
            payload={},
        )
        self.store.record_learning_proposal(proposal)
        self.store.record_event(
            state.run_id,
            state.step,
            "learning_review",
            {
                "proposal_id": proposal.id,
                "proposal_type": proposal.proposal_type.value,
                "status": proposal.status.value,
                "reason": proposal.reason,
            },
        )
        return proposal
