from __future__ import annotations

import uuid

from .action_gateway import ActionGateway
from .learning import LearningLoop
from .model import CandidateModel
from .models import (
    CandidatePath,
    Goal,
    GoalStatus,
    Outcome,
    ProcedureSkill,
    ProcedureSkillSummary,
    RunResult,
    RunState,
    TerminationReason,
    Transition,
)
from .outcome import OutcomeEvaluator
from .procedure_skills import ProcedureSkillLibrary
from .project_context import ProjectContext
from .recipes import RecipeEngine
from .selection import CandidateSelector
from .skill_curator import CuratorDaemon
from .store import AutonomyStore, format_memory_context
from .tools.redaction import redact_jsonable


class AgentLoop:
    """Self-directed task loop. Action execution is delegated to ActionGateway."""

    def __init__(
        self,
        *,
        model: CandidateModel,
        action_gateway: ActionGateway,
        outcome_evaluator: OutcomeEvaluator,
        store: AutonomyStore,
        selector: CandidateSelector | None = None,
        recipes: RecipeEngine | None = None,
        procedure_skills: ProcedureSkillLibrary | None = None,
        learning_loop: LearningLoop | None = None,
        curator_daemon: CuratorDaemon | None = None,
        project_context: ProjectContext | None = None,
    ):
        self.model = model
        self.action_gateway = action_gateway
        self.outcome_evaluator = outcome_evaluator
        self.store = store
        self.selector = selector or CandidateSelector(beam_width=3)
        self.recipes = recipes or RecipeEngine(store)
        self.procedure_skills = procedure_skills
        self.learning_loop = learning_loop or LearningLoop(
            model=model,
            store=store,
            procedure_skills=procedure_skills,
        )
        self.curator_daemon = curator_daemon
        self.project_context = project_context

    @property
    def tools(self):
        return self.action_gateway.tools

    def run(
        self,
        goal: str,
        max_steps: int = 12,
        interactive: bool = True,
        interface: str = "run",
        conversation_context: str = "",
        journal_metadata: dict | None = None,
    ) -> RunResult:
        state = self.start_run(
            goal,
            max_steps=max_steps,
            interface=interface,
            conversation_context=conversation_context,
            journal_metadata=journal_metadata,
        )

        try:
            for step in range(1, max_steps + 1):
                state.step = step
                result = self.run_turn(state, interactive=interactive)
                if result is not None:
                    return result
            return self.finish_run(
                state,
                TerminationReason.MAX_STEPS_REACHED,
                f"maximum step count reached: {max_steps}",
            )
        except Exception as exc:
            self.store.record_event(state.run_id, state.step, "run_error", {"error": str(exc)})
            return self.finish_run(state, TerminationReason.FAILED, str(exc))

    def start_run(
        self,
        goal: str,
        *,
        max_steps: int,
        interface: str,
        conversation_context: str = "",
        journal_metadata: dict | None = None,
    ) -> RunState:
        if not goal.strip():
            raise ValueError("goal must not be empty")
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if not interface.strip():
            raise ValueError("interface must not be empty")

        relevant_memories = self.store.search_memories(goal.strip(), limit=5)
        memory_context = format_memory_context(
            "Relevant persistent memory:",
            relevant_memories,
        )
        context_parts = [
            part
            for part in (conversation_context.strip(), memory_context)
            if part
        ]
        state = RunState(
            run_id=uuid.uuid4().hex,
            goal=Goal(goal.strip()),
            conversation_context="\n\n".join(context_parts),
            project_context=self.project_context.content if self.project_context else "",
        )
        self.store.create_run(state.run_id, state.goal.text)
        model_context = getattr(self.model, "journal_context", {})
        self.store.record_event(
            state.run_id,
            0,
            "run_started",
            {
                "goal": state.goal.text,
                "interface": interface.strip(),
                **model_context,
                **(journal_metadata or {}),
            },
        )
        if self.project_context:
            self.store.record_event(
                state.run_id,
                0,
                "project_context_loaded",
                {
                    "source": self.project_context.source,
                    "chars": len(self.project_context.content),
                    "original_chars": self.project_context.original_chars,
                    "truncated": self.project_context.truncated,
                },
            )
        if relevant_memories:
            self.store.record_event(
                state.run_id,
                0,
                "memory_loaded",
                {
                    "count": len(relevant_memories),
                    "ids": [memory["id"] for memory in relevant_memories],
                },
            )
        return state

    def run_turn(self, state: RunState, *, interactive: bool) -> RunResult | None:
        loaded_skills = self.disclose_procedure_skills(state)
        candidates = self.generate_candidates(state, loaded_skills)
        ranked = self.rank_candidates(state, candidates)
        if not ranked:
            return self.finish_run(
                state,
                TerminationReason.NO_CANDIDATES,
                "no candidates were generated",
            )

        gateway_result = self.action_gateway.execute_next(
            state,
            ranked,
            interactive=interactive,
        )
        if gateway_result.action is None:
            return self.finish_run(
                state,
                TerminationReason.NO_CANDIDATES,
                gateway_result.approval_reason,
            )
        if not gateway_result.approval_allowed:
            return self.finish_run(
                state,
                TerminationReason.APPROVAL_DENIED,
                gateway_result.approval_reason,
            )
        if gateway_result.observation is None:
            return self.finish_run(
                state,
                TerminationReason.FAILED,
                "action gateway did not return an observation",
            )

        outcome = self.evaluate_outcome(
            state,
            gateway_result.action,
            gateway_result.observation,
        )
        transition = Transition(
            state.run_id,
            state.step,
            gateway_result.action,
            gateway_result.observation,
            outcome,
        )
        self.store.record_transition(transition)
        state.transitions.append(transition)
        self.learn_from_transition(state, transition)
        state.current_state = outcome.reason

        if outcome.goal_status == GoalStatus.ACHIEVED:
            return self.finish_run(state, TerminationReason.ACHIEVED, outcome.reason)
        if outcome.goal_status == GoalStatus.BLOCKED:
            return self.finish_run(state, TerminationReason.BLOCKED, outcome.reason)
        return None

    def disclose_procedure_skills(self, state: RunState) -> list[ProcedureSkill]:
        considered_skills: list[ProcedureSkillSummary] = (
            self.procedure_skills.index(self.tools.names)
            if self.procedure_skills
            else []
        )
        self.store.record_event(
            state.run_id,
            state.step,
            "skills_considered",
            considered_skills,
        )
        selected_skill_names = (
            self.model.select_procedure_skills(
                state,
                considered_skills,
                self.tools.names,
            )
            if considered_skills
            else []
        )
        self.store.record_event(
            state.run_id,
            state.step,
            "skills_selected",
            selected_skill_names,
        )
        loaded_skills = (
            self.procedure_skills.load_selected(
                selected_skill_names,
                self.tools.names,
            )
            if self.procedure_skills
            else []
        )
        self.store.record_event(
            state.run_id,
            state.step,
            "skills_loaded",
            [skill.summary for skill in loaded_skills],
        )
        return loaded_skills

    def generate_candidates(
        self,
        state: RunState,
        loaded_skills: list[ProcedureSkill],
    ) -> list[CandidatePath]:
        candidates = [
            *self.model.propose(
                state,
                self.tools.names,
                loaded_skills,
                tool_specs=self.tools.model_specs(),
            ),
            *self.recipes.candidates_for(state),
        ]
        self.store.record_event(state.run_id, state.step, "action_intents_generated", candidates)
        return candidates

    def rank_candidates(
        self,
        state: RunState,
        candidates: list[CandidatePath],
    ) -> list[CandidatePath]:
        completed_action_fingerprints = {
            transition.action.fingerprint
            for transition in state.transitions
            if transition.observation.succeeded and transition.outcome.execution_ok
        }
        failed_action_counts: dict[str, int] = {}
        for transition in state.transitions:
            if transition.observation.succeeded and transition.outcome.execution_ok:
                continue
            fingerprint = transition.action.fingerprint
            failed_action_counts[fingerprint] = failed_action_counts.get(fingerprint, 0) + 1
        ranked = self.selector.select(
            candidates,
            self.tools.names,
            completed_action_fingerprints,
            failed_action_counts,
            self.tools.rejection_reason,
            self.action_gateway.risk_for_intent,
            self.action_gateway.side_effects_for_intent,
        )
        self.store.record_event(
            state.run_id,
            state.step,
            "candidates_penalized",
            [
                {
                    "candidate_id": candidate.id,
                    "source": candidate.source,
                    "reasons": candidate.penalty_reasons,
                    "score": candidate.score,
                    "score_details": candidate.score_details,
                }
                for candidate in candidates
                if candidate.penalty_reasons
            ],
        )
        self.store.record_event(state.run_id, state.step, "candidates_ranked", ranked)
        return ranked

    def evaluate_outcome(
        self,
        state: RunState,
        action,
        observation,
    ) -> Outcome:
        outcome = self.outcome_evaluator.evaluate(state, action, observation)
        self.store.record_event(state.run_id, state.step, "outcome_evaluated", outcome)
        return outcome

    def learn_from_transition(self, state: RunState, transition: Transition) -> None:
        learning_result = self.recipes.learn(transition)
        if learning_result:
            if learning_result.created:
                redacted_arguments, _ = redact_jsonable(transition.action.arguments)
                self.store.create_memory(
                    scope="workspace",
                    wing="workflow",
                    room="repeated-action",
                    content=(
                        "Repeated successful action: "
                        f"{transition.action.tool} {redacted_arguments} "
                        f"purpose={transition.action.purpose or transition.action.expected_effect}"
                    ),
                    source_run_id=state.run_id,
                )
            self.store.record_event(
                state.run_id,
                state.step,
                "candidate_recipe_learned",
                {
                    "created": learning_result.created,
                    "recipe": learning_result.recipe,
                },
            )

    def finish_run(
        self,
        state: RunState,
        termination: TerminationReason,
        reason: str,
    ) -> RunResult:
        try:
            self.learning_loop.review_run(state, termination=termination, reason=reason)
        except Exception as exc:
            self.store.record_event(
                state.run_id,
                state.step,
                "learning_review_error",
                {"error": str(exc)},
            )
        if self.curator_daemon:
            try:
                self.curator_daemon.trigger_after_run(state.run_id)
            except Exception as exc:
                self.store.record_event(
                    state.run_id,
                    state.step,
                    "curator_daemon_error",
                    {"error": str(exc)},
                )
        try:
            self.tools.close()
        except Exception as exc:
            self.store.record_event(
                state.run_id,
                state.step,
                "tool_cleanup_error",
                {"error": str(exc)},
            )
        result = RunResult(
            run_id=state.run_id,
            goal=state.goal.text,
            termination=termination,
            steps_executed=len(state.transitions),
            reason=reason,
        )
        self.store.record_event(state.run_id, state.step, "run_finished", result)
        self.store.complete_run(result)
        return result
