from __future__ import annotations

from dataclasses import dataclass

from .models import Action, ActionIntent, CandidatePath, Observation, RiskLevel, RunState
from .store import AutonomyStore
from .tools import ApprovalPolicy, ToolRegistry


@dataclass(frozen=True)
class ActionGatewayResult:
    action: Action | None
    observation: Observation | None
    approval_allowed: bool
    approval_reason: str
    blocked: list[dict[str, str]]


class ActionGateway:
    """Governed action execution boundary shared by agent loops."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        store: AutonomyStore,
        approval: ApprovalPolicy | None = None,
    ):
        self.tools = tools
        self.store = store
        self.approval = approval or ApprovalPolicy()

    def execute_next(
        self,
        state: RunState,
        ranked: list[CandidatePath],
        *,
        interactive: bool,
    ) -> ActionGatewayResult:
        action, blocked = self.choose_executable_action(ranked)
        if blocked:
            self.store.record_event(
                state.run_id,
                state.step,
                "execution_candidates_blocked",
                blocked,
            )
        if action is None:
            return ActionGatewayResult(
                action=None,
                observation=None,
                approval_allowed=True,
                approval_reason="no ranked candidate passed execution boundary validation",
                blocked=blocked,
            )

        self.store.record_event(
            state.run_id,
            state.step,
            "action_selected",
            {
                "tool": action.tool,
                "arguments": action.arguments,
                "purpose": action.purpose,
                "risk_level": action.risk_level.value,
                "effective_risk_level": self.approval.effective_risk(action).value,
                "expected_effect": action.expected_effect,
                "verification_plan": action.verification_plan,
                "tool_spec": self.tools.spec(action.tool).summary,
            },
        )
        allowed, approval_reason = self.authorize_action(
            state,
            action,
            interactive=interactive,
        )
        if not allowed:
            return ActionGatewayResult(
                action=action,
                observation=None,
                approval_allowed=False,
                approval_reason=approval_reason,
                blocked=blocked,
            )

        observation = self.execute_action(state, action)
        return ActionGatewayResult(
            action=action,
            observation=observation,
            approval_allowed=True,
            approval_reason=approval_reason,
            blocked=blocked,
        )

    def choose_executable_action(
        self,
        ranked: list[CandidatePath],
    ) -> tuple[Action | None, list[dict[str, str]]]:
        blocked: list[dict[str, str]] = []
        for candidate in ranked:
            if not candidate.actions:
                blocked.append(
                    {
                        "candidate_id": candidate.id,
                        "source": candidate.source,
                        "reason": "candidate has no actions",
                    }
                )
                continue
            intent = candidate.next_action
            if intent.tool not in self.tools.names:
                blocked.append(
                    {
                        "candidate_id": candidate.id,
                        "source": candidate.source,
                        "reason": f"tool is unavailable: {intent.tool}",
                    }
                )
                continue
            reason = self.tools.rejection_reason(intent)
            if reason:
                blocked.append(
                    {
                        "candidate_id": candidate.id,
                        "source": candidate.source,
                        "reason": reason,
                    }
                )
                continue
            action = self.tools.action_from_intent(intent)
            return action, blocked
        return None, blocked

    def authorize_action(
        self,
        state: RunState,
        action: Action,
        *,
        interactive: bool,
    ) -> tuple[bool, str]:
        allowed, approval_reason = self.approval.authorize(action, interactive)
        self.store.record_event(
            state.run_id,
            state.step,
            "approval_decision",
            {"allowed": allowed, "reason": approval_reason},
        )
        return allowed, approval_reason

    def execute_action(self, state: RunState, action: Action) -> Observation:
        observation = self.tools.execute(action)
        self.store.record_event(
            state.run_id,
            state.step,
            "observation",
            observation,
        )
        return observation

    def risk_for_intent(self, intent: ActionIntent) -> RiskLevel:
        if intent.tool not in self.tools.names:
            return RiskLevel.HIGH
        return self.tools.spec(intent.tool).default_risk

    def side_effects_for_intent(self, intent: ActionIntent) -> tuple[str, ...]:
        if intent.tool not in self.tools.names:
            return ("unknown-tool",)
        return self.tools.spec(intent.tool).side_effects
