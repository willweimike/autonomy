from __future__ import annotations

import uuid
from pathlib import Path
from typing import Callable, Protocol

from .models import ConversationResponse, ConversationTurn, RunResult
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
        store: AutonomyStore | None = None,
        session_id: str | None = None,
        recent_turn_limit: int = 6,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if recent_turn_limit < 1:
            raise ValueError("recent_turn_limit must be at least 1")
        self.workspace = workspace.resolve()
        self.db_path = db_path
        self.max_steps = max_steps
        self.agent_loop_factory = agent_loop_factory
        self.store = store or AutonomyStore(db_path)
        self.session_id = session_id or uuid.uuid4().hex
        self.recent_turn_limit = recent_turn_limit
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

        agent_loop = self.agent_loop_factory(self.workspace, self.db_path)
        result = agent_loop.run(
            goal,
            max_steps=self.max_steps,
            interactive=True,
            interface="chat",
            conversation_context=conversation_context,
            journal_metadata={
                "conversation_session_id": self.session_id,
                "conversation_turn_id": user_turn_id,
            },
        )
        self.last_run_result = result
        self.store.link_conversation_turn_run(user_turn_id, result.run_id)
        candidate_skills = tuple(self._candidate_skills_for_run(result.run_id))

        reply = self._format_reply(result)
        assistant_turn_id = uuid.uuid4().hex
        self.store.record_conversation_turn(
            ConversationTurn(
                id=assistant_turn_id,
                session_id=self.session_id,
                role="assistant",
                content=reply,
                run_id=result.run_id,
                metadata={
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

    @staticmethod
    def _format_reply(result: RunResult) -> str:
        return "\n".join(
            [
                f"run_id: {result.run_id}",
                f"termination: {result.termination.value}",
                f"steps: {result.steps_executed}",
                f"reason: {result.reason}",
            ]
        )
