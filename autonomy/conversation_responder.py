from __future__ import annotations

from typing import Protocol

from .models import RunResult
from .providers import ProviderConfigurationError


class ConversationResponder(Protocol):
    def respond_to_chat(self, conversation_context: str, user_input: str) -> str:
        ...

    def summarize_task_result(
        self,
        conversation_context: str,
        user_input: str,
        result: RunResult,
    ) -> str:
        ...


class ModelConversationResponder:
    """Generate conversation text after routing or task execution has completed."""

    def __init__(self, model):
        self.model = model

    def respond_to_chat(self, conversation_context: str, user_input: str) -> str:
        return self.model.respond_to_chat(conversation_context, user_input)

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

    def respond_to_chat(self, conversation_context: str, user_input: str) -> str:
        del conversation_context, user_input
        raise self.error

    def summarize_task_result(
        self,
        conversation_context: str,
        user_input: str,
        result: RunResult,
    ) -> str:
        del conversation_context, user_input, result
        raise self.error
