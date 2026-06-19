from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Protocol

from .conversation_responder import ConversationResponder
from .models import ConversationDecision, ConversationMode, ConversationResponse, ConversationTurn, RunResult, TerminationReason
from .providers import ModelClientError, ProviderConfigurationError
from .store import AutonomyStore, format_memory_context


class AgentLoopRunner(Protocol):
    def run(
        self,
        goal: str,
        max_steps: int = 12,
        interactive: bool = True,
        interface: str = "run",
        conversation_context: str = "",
        journal_metadata: dict | None = None,
    ) -> RunResult:
        ...


AgentLoopFactory = Callable[[Path, Path], AgentLoopRunner]


class ConversationLoop:
    """Session-level conversation continuity above the task agent loop."""

    def __init__(
        self,
        *,
        workspace: Path,
        db_path: Path,
        max_steps: int,
        agent_loop_factory: AgentLoopFactory,
        responder: ConversationResponder,
        store: AutonomyStore | None = None,
        session_id: str | None = None,
        recent_turn_limit: int = 6,
        interface: str = "tui",
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if recent_turn_limit < 1:
            raise ValueError("recent_turn_limit must be at least 1")
        if not interface.strip():
            raise ValueError("interface must not be empty")
        self.workspace = workspace.resolve()
        self.db_path = db_path
        self.max_steps = max_steps
        self.agent_loop_factory = agent_loop_factory
        self.responder = responder
        self.store = store or AutonomyStore(db_path)
        self.session_id = session_id or uuid.uuid4().hex
        self.recent_turn_limit = recent_turn_limit
        self.interface = interface.strip()
        self.last_run_result: RunResult | None = None
        self.store.create_conversation_session(self.session_id, str(self.workspace))
        self.startup_memory_context = format_memory_context(
            "Persistent memory loaded at session start:",
            self.store.list_memories(limit=10),
        )

    def set_workspace(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.store.update_conversation_workspace(self.session_id, str(self.workspace))

    def set_max_steps(self, max_steps: int) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.max_steps = max_steps

    def handle_user_input(self, text: str) -> ConversationResponse:
        goal = text.strip()
        if not goal:
            raise ValueError("conversation input must not be empty")

        conversation_context = self._build_context()
        user_turn_id = uuid.uuid4().hex
        self.store.record_conversation_turn(
            ConversationTurn(
                id=user_turn_id,
                session_id=self.session_id,
                role="user",
                content=goal,
            )
        )

        decision = ConversationDecision(
            mode=ConversationMode.TASK,
            task_goal=goal,
            reason="agent turn",
        )
        task_goal = goal
        agent_loop = self.agent_loop_factory(self.workspace, self.db_path)
        result = agent_loop.run(
            task_goal,
            max_steps=self.max_steps,
            interactive=True,
            interface=self.interface,
            conversation_context=conversation_context,
            journal_metadata={
                "conversation_session_id": self.session_id,
                "conversation_turn_id": user_turn_id,
            },
        )
        self.last_run_result = result
        self.store.link_conversation_turn_run(user_turn_id, result.run_id)
        candidate_skills = tuple(self._candidate_skills_for_run(result.run_id))
        action_recipe_candidates = tuple(self._action_recipe_candidates_for_run(result.run_id))

        reply = self._format_task_reply(
            result,
            decision,
            conversation_context=conversation_context,
            user_input=goal,
        )
        assistant_turn_id = uuid.uuid4().hex
        self.store.record_conversation_turn(
            ConversationTurn(
                id=assistant_turn_id,
                session_id=self.session_id,
                role="assistant",
                content=reply,
                run_id=result.run_id,
                metadata={
                    "mode": decision.mode.value,
                    "conversation_reason": decision.reason,
                    "termination": result.termination.value,
                    "steps_executed": result.steps_executed,
                    "reason": result.reason,
                },
            )
        )
        return ConversationResponse(
            session_id=self.session_id,
            user_turn_id=user_turn_id,
            assistant_turn_id=assistant_turn_id,
            run_result=result,
            reply=reply,
            conversation_context=conversation_context,
            candidate_skills=candidate_skills,
            action_recipe_candidates=action_recipe_candidates,
            decision=decision,
        )

    def _build_context(self) -> str:
        sections: list[str] = []
        if self.startup_memory_context:
            sections.append(self.startup_memory_context)
        turns = self.store.list_conversation_turns(
            self.session_id,
            limit=self.recent_turn_limit,
        )
        if not turns:
            return "\n\n".join(sections)
        lines = ["Recent conversation context:"]
        for turn in turns:
            content = turn.content.strip()
            if len(content) > 1200:
                content = content[:1200] + "..."
            run_suffix = f" run_id={turn.run_id}" if turn.run_id else ""
            lines.append(f"- {turn.role}{run_suffix}: {content}")
        sections.append("\n".join(lines))
        return "\n\n".join(sections)

    def _candidate_skills_for_run(self, run_id: str) -> list[dict]:
        try:
            journal = self.store.inspect_run(run_id)
        except KeyError:
            return []
        candidates: list[dict] = []
        for event in journal["events"]:
            if event["event_type"] == "procedure_skill_candidate_created":
                payload = event["payload"]
                if isinstance(payload, dict):
                    candidates.append(payload)
        return candidates

    def _action_recipe_candidates_for_run(self, run_id: str) -> list[dict]:
        try:
            journal = self.store.inspect_run(run_id)
        except KeyError:
            return []
        candidates: list[dict] = []
        for event in journal["events"]:
            if event["event_type"] != "candidate_recipe_learned":
                continue
            payload = event["payload"]
            if not isinstance(payload, dict) or payload.get("created") is not True:
                continue
            recipe = payload.get("recipe")
            if isinstance(recipe, dict):
                candidates.append(recipe)
            if len(candidates) >= 3:
                break
        return candidates

    def _format_task_reply(
        self,
        result: RunResult,
        decision,
        *,
        conversation_context: str,
        user_input: str,
    ) -> str:
        del decision
        assistant_response = self._assistant_response_for_run(result.run_id)
        if assistant_response:
            return assistant_response
        if result.termination == TerminationReason.FAILED and result.steps_executed == 0:
            summary = (
                "I could not start the task because the model did not return valid structured "
                f"JSON for planning. No tool action was executed. Reason: {result.reason}"
            )
        elif result.termination != TerminationReason.ACHIEVED:
            summary = self._deterministic_incomplete_summary(result)
        else:
            try:
                summary = self.responder.summarize_task_result(
                    conversation_context,
                    user_input,
                    result,
                )
            except (ModelClientError, ProviderConfigurationError, ValueError) as exc:
                summary = f"task summary response error: {exc}"
        return "\n".join(
            [
                summary,
                "",
                f"run_id: {result.run_id}",
                f"termination: {result.termination.value}",
                f"steps: {result.steps_executed}",
            ]
        )

    def _deterministic_incomplete_summary(self, result: RunResult) -> str:
        lines = [
            "Task did not complete.",
            f"termination: {result.termination.value}",
            f"reason: {result.reason}",
        ]
        try:
            journal = self.store.inspect_run(result.run_id)
        except KeyError:
            return "\n".join(lines)

        selected_action: dict | None = None
        latest_observation: dict | None = None
        for event in journal["events"]:
            payload = event["payload"]
            if event["event_type"] == "action_selected" and isinstance(payload, dict):
                selected_action = payload
            elif event["event_type"] == "observation" and isinstance(payload, dict):
                latest_observation = payload

        if selected_action:
            tool = str(selected_action.get("tool", "")).strip()
            purpose = str(selected_action.get("purpose", "")).strip()
            action_line = f"selected action: {tool}" if tool else "selected action: unknown"
            if purpose:
                action_line += f" · {purpose}"
            lines.append(action_line)
        if latest_observation:
            succeeded = latest_observation.get("succeeded") is True
            lines.append(f"observation: {'succeeded' if succeeded else 'failed'}")
            if latest_observation.get("exit_code") is not None:
                lines.append(f"exit_code: {latest_observation['exit_code']}")
            error = str(latest_observation.get("error", "")).strip()
            output = str(latest_observation.get("output", "")).strip()
            if error:
                lines.append(f"error: {self._short_observation_text(error)}")
            if output:
                lines.append(f"output: {self._short_observation_text(output)}")
        return "\n".join(lines)

    @staticmethod
    def _short_observation_text(text: str, limit: int = 500) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "..."

    def _assistant_response_for_run(self, run_id: str) -> str:
        try:
            journal = self.store.inspect_run(run_id)
        except KeyError:
            return ""
        selected_assistant_response = False
        for event in journal["events"]:
            event_type = event["event_type"]
            payload = event["payload"]
            if event_type == "action_selected" and isinstance(payload, dict):
                selected_assistant_response = payload.get("tool") == "assistant.respond"
            elif (
                selected_assistant_response
                and event_type == "observation"
                and isinstance(payload, dict)
                and payload.get("succeeded") is True
            ):
                return str(payload.get("output", "")).strip()
        return ""
