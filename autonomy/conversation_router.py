from __future__ import annotations

from typing import Protocol

from .model import CandidateModel
from .models import ConversationDecision, ConversationMode
from .providers import ModelClientError, ProviderConfigurationError


class ConversationRouter(Protocol):
    def route(self, conversation_context: str, user_input: str) -> ConversationDecision:
        ...


class ModelConversationRouter:
    """Classify conversation turns before deciding whether to start a task run."""

    def __init__(self, model: CandidateModel):
        self.model = model

    def route(self, conversation_context: str, user_input: str) -> ConversationDecision:
        text = user_input.strip()
        try:
            return self.model.classify_conversation_turn(conversation_context, text)
        except (ModelClientError, AttributeError, KeyError, TypeError, ValueError) as exc:
            return ConversationDecision(
                mode=ConversationMode.TASK,
                task_goal=text,
                reason=f"conversation router failed; falling back to task mode: {exc}",
            )


class MissingModelConversationRouter:
    def __init__(self, error: ProviderConfigurationError):
        self.error = error

    def route(self, conversation_context: str, user_input: str) -> ConversationDecision:
        del conversation_context, user_input
        raise self.error
