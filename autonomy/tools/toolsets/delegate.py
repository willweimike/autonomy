from __future__ import annotations

from collections.abc import Callable

from ...delegation import AgentExecutionContext, current_agent_execution_context
from ...models import Observation, RiskLevel
from ..registry import ToolRegistry


DelegateRunner = Callable[[str, int, AgentExecutionContext], Observation]


def register_delegate_tools(registry: ToolRegistry, delegate_runner: DelegateRunner) -> None:
    def delegate_run(arguments: dict) -> Observation:
        context = current_agent_execution_context()
        if context is None:
            return Observation("", False, error="delegate.run requires an active parent run")
        goal = _delegate_goal(arguments)
        max_steps = _delegate_max_steps(arguments, default=context.max_steps)
        return delegate_runner(goal, max_steps, context)

    registry.register(
        "delegate.run",
        delegate_run,
        validate_delegate_run,
        description="Run an unrestricted child AgentLoop for a delegated subtask.",
        toolset="delegate",
        argument_contract={
            "goal": "string child task goal",
            "max_steps": "integer child max steps, default parent max_steps (optional)",
        },
        default_risk=RiskLevel.LOW,
        side_effects=("child-agent-run",),
    )


def validate_delegate_run(arguments: dict) -> None:
    _delegate_goal(arguments)
    if "max_steps" in arguments:
        _delegate_max_steps(arguments, default=1)


def _delegate_goal(arguments: dict) -> str:
    goal = str(arguments.get("goal", "")).strip()
    if not goal:
        raise ValueError("goal must not be empty")
    return goal


def _delegate_max_steps(arguments: dict, *, default: int) -> int:
    value = int(arguments["max_steps"]) if "max_steps" in arguments else default
    if value < 1:
        raise ValueError("max_steps must be at least 1")
    return value
