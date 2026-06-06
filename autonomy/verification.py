from __future__ import annotations

from typing import Protocol

from .models import Action, Observation, RunState, Verification


class Verifier(Protocol):
    def verify(self, state: RunState, action: Action, observation: Observation) -> Verification:
        ...


class VerificationModel(Protocol):
    def verify(self, state: RunState, action: Action, observation: Observation) -> Verification:
        ...


class DeterministicVerifier:
    """Deterministic evidence always outranks model-level interpretation."""

    def verify(self, state: RunState, action: Action, observation: Observation) -> Verification:
        if not observation.succeeded:
            return Verification(
                verified=True,
                goal_achieved=False,
                continue_allowed=False,
                reason=observation.error or "action failed",
                evidence=observation.evidence,
            )
        goal_achieved = bool(action.arguments.get("_goal_achieving", False))
        return Verification(
            verified=True,
            goal_achieved=goal_achieved,
            continue_allowed=not goal_achieved,
            reason="deterministic execution evidence accepted",
            evidence=observation.evidence,
            progress=max(0.0, min(action.goal_progress, 1.0)),
        )


class ModelAssistedVerifier:
    """Use model judgment only after deterministic execution evidence succeeds."""

    def __init__(self, model: VerificationModel):
        self.model = model
        self.deterministic = DeterministicVerifier()

    def verify(self, state: RunState, action: Action, observation: Observation) -> Verification:
        deterministic = self.deterministic.verify(state, action, observation)
        if not observation.succeeded or deterministic.goal_achieved:
            return deterministic
        return self.model.verify(state, action, observation)
