from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Protocol

from .conversation_responder import ConversationResponder
from .conversation_router import ConversationRouter
from .models import ConversationMode, ConversationResponse, ConversationTurn, RunResult, TerminationReason
from .providers import ModelClientError, ProviderConfigurationError
from .store import AutonomyStore


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
        router: ConversationRouter,
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
        self.router = router
        self.responder = responder
        self.store = store or AutonomyStore(db_path)
        self.session_id = session_id or uuid.uuid4().hex
        self.recent_turn_limit = recent_turn_limit
        self.interface = interface.strip()
        self.last_run_result: RunResult | None = None
        self.store.create_conversation_session(self.session_id, str(self.workspace))

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

        decision = self.router.route(conversation_context, goal)
        if decision.mode == ConversationMode.CHAT:
            reply = self._safe_chat_reply(conversation_context, goal)
            assistant_turn_id = uuid.uuid4().hex
            self.store.record_conversation_turn(
                ConversationTurn(
                    id=assistant_turn_id,
                    session_id=self.session_id,
                    role="assistant",
                    content=reply,
                    metadata={
                        "mode": decision.mode.value,
                        "reason": decision.reason,
                    },
                )
            )
            return ConversationResponse(
                session_id=self.session_id,
                user_turn_id=user_turn_id,
                assistant_turn_id=assistant_turn_id,
                run_result=None,
                reply=reply,
                conversation_context=conversation_context,
                decision=decision,
            )

        task_goal = decision.task_goal.strip() or goal
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
        turns = self.store.list_conversation_turns(
            self.session_id,
            limit=self.recent_turn_limit,
        )
        if not turns:
            return ""
        lines = ["Recent conversation context:"]
        for turn in turns:
            content = turn.content.strip()
            if len(content) > 1200:
                content = content[:1200] + "..."
            run_suffix = f" run_id={turn.run_id}" if turn.run_id else ""
            lines.append(f"- {turn.role}{run_suffix}: {content}")
        return "\n".join(lines)

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

    def _safe_chat_reply(self, conversation_context: str, user_input: str) -> str:
        try:
            return self.responder.respond_to_chat(conversation_context, user_input)
        except (ModelClientError, ProviderConfigurationError, ValueError) as exc:
            return f"conversation response error: {exc}"

    def _format_task_reply(
        self,
        result: RunResult,
        decision,
        *,
        conversation_context: str,
        user_input: str,
    ) -> str:
        del decision
        if result.termination == TerminationReason.FAILED and result.steps_executed == 0:
            summary = (
                "I could not start the task because the model did not return valid structured "
                f"JSON for planning. No tool action was executed. Reason: {result.reason}"
            )
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
