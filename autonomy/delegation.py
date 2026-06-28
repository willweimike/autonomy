from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


_EXPLICIT_SUBAGENT_MARKERS = (
    "subagent",
    "sub-agent",
    "parallel agent",
    "parallel agents",
    "spawn agent",
    "spawn agents",
    "one agent per",
    "delegate this",
    "子代理",
    "子 agent",
    "平行代理",
    "平行 agent",
    "啟動 subagent",
    "派 subagent",
    "委派",
    "分派代理",
)


@dataclass(frozen=True)
class AgentExecutionContext:
    run_id: str
    step: int
    max_steps: int


_CURRENT_AGENT_CONTEXT: ContextVar[AgentExecutionContext | None] = ContextVar(
    "autonomy_agent_execution_context",
    default=None,
)


def current_agent_execution_context() -> AgentExecutionContext | None:
    return _CURRENT_AGENT_CONTEXT.get()


def set_agent_execution_context(context: AgentExecutionContext) -> Token:
    return _CURRENT_AGENT_CONTEXT.set(context)


def reset_agent_execution_context(token: Token) -> None:
    _CURRENT_AGENT_CONTEXT.reset(token)


def subagents_explicitly_requested(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _EXPLICIT_SUBAGENT_MARKERS)


def subagents_allowed_for(goal: str, interface: str) -> bool:
    return interface.strip() == "delegate" or subagents_explicitly_requested(goal)
