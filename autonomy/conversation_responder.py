from __future__ import annotations

from typing import Protocol

from .models import RunResult
from .providers import ProviderConfigurationError


class ConversationResponder(Protocol):
    def summarize_task_result(
        self,
        conversation_context: str,
        user_input: str,
        result: RunResult,
    ) -> str:
        ...


class ModelConversationResponder:
    """Generate conversation text after task execution has completed."""

    def __init__(self, model):
        self.model = model

    def summarize_task_result(
        self,
        conversation_context: str,
        user_input: str,
        result: RunResult,
    ) -> str:
        return self.model.summarize_task_result(conversation_context, user_input, result)


class MissingModelConversationResponder:
    def __init__(self, error: ProviderConfigurationError):
        self.error = error

    def summarize_task_result(
        self,
        conversation_context: str,
        user_input: str,
        result: RunResult,
    ) -> str:
        del conversation_context, user_input, result
        raise self.error
