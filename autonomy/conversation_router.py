from __future__ import annotations

from typing import Protocol

from .model import CandidateModel
from .models import ConversationDecision
from .providers import ProviderConfigurationError


class ConversationRouter(Protocol):
    def route(self, conversation_context: str, user_input: str) -> ConversationDecision:
        ...


class ModelConversationRouter:
    """Legacy model-backed conversation turn classifier."""

    def __init__(self, model: CandidateModel):
        self.model = model

    def route(self, conversation_context: str, user_input: str) -> ConversationDecision:
        text = user_input.strip()
        return self.model.classify_conversation_turn(conversation_context, text)


class MissingModelConversationRouter:
    def __init__(self, error: ProviderConfigurationError):
        self.error = error

    def route(self, conversation_context: str, user_input: str) -> ConversationDecision:
        del conversation_context, user_input
        raise self.error
