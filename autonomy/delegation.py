from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


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

