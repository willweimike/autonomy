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
                reason=self._failed_observation_reason(action, observation),
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

    @staticmethod
    def _failed_observation_reason(action: Action, observation: Observation) -> str:
        parts = [f"{action.tool} failed"]
        if observation.exit_code is not None:
            parts.append(f"exit_code {observation.exit_code}")
        if observation.error.strip():
            parts.append("error: " + DeterministicOutcomeEvaluator._short_text(observation.error))
        elif observation.output.strip():
            parts.append("output: " + DeterministicOutcomeEvaluator._short_text(observation.output))
        return "; ".join(parts)

    @staticmethod
    def _short_text(text: str, limit: int = 300) -> str:
        text = text.strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "..."


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
