from __future__ import annotations

from typing import Protocol

from .models import Action, GoalStatus, Observation, Outcome, RunState


class OutcomeEvaluator(Protocol):
    def evaluate(self, state: RunState, action: Action, observation: Observation) -> Outcome:
        ...


class OutcomeModel(Protocol):
    def evaluate_outcome(
        self,
        state: RunState,
        action: Action,
        observation: Observation,
    ) -> Outcome:
        ...


class DeterministicOutcomeEvaluator:
    """Agent-side outcome rules always outrank model-level interpretation."""

    def evaluate(self, state: RunState, action: Action, observation: Observation) -> Outcome:
        del state
        if not observation.succeeded:
            return Outcome(
                execution_ok=False,
                goal_status=GoalStatus.BLOCKED,
                reason=observation.error or "action failed",
                evidence=observation.evidence,
                confidence=1.0,
            )
        if bool(action.arguments.get("_goal_achieving", False)):
            return Outcome(
                execution_ok=True,
                goal_status=GoalStatus.ACHIEVED,
                reason="deterministic goal-achieving evidence accepted",
                evidence=observation.evidence,
                confidence=1.0,
            )
        if action.tool == "assistant.respond":
            return Outcome(
                execution_ok=True,
                goal_status=GoalStatus.ACHIEVED,
                reason="assistant response returned",
                evidence=observation.evidence,
                confidence=1.0,
            )
        return Outcome(
            execution_ok=True,
            goal_status=GoalStatus.CONTINUE,
            reason="deterministic execution evidence accepted",
            evidence=observation.evidence,
            confidence=1.0,
        )


class ModelAssistedOutcomeEvaluator:
    """Use model judgment only after deterministic execution evidence succeeds."""

    def __init__(self, model: OutcomeModel):
        self.model = model
        self.deterministic = DeterministicOutcomeEvaluator()

    def evaluate(self, state: RunState, action: Action, observation: Observation) -> Outcome:
        deterministic = self.deterministic.evaluate(state, action, observation)
        if not observation.succeeded or deterministic.goal_status == GoalStatus.ACHIEVED:
            return deterministic
        return self.model.evaluate_outcome(state, action, observation)
