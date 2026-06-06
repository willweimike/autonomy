from __future__ import annotations

import uuid

from .model import CandidateModel
from .models import (
    Action,
    ActionIntent,
    CandidatePath,
    Goal,
    Observation,
    ProcedureSkill,
    ProcedureSkillSummary,
    RiskLevel,
    RunResult,
    RunState,
    TerminationReason,
    Transition,
    Verification,
)
from .procedure_skills import ProcedureSkillLibrary
from .recipes import RecipeEngine
from .selection import CandidateSelector
from .store import AutonomyStore
from .tools import ApprovalPolicy, ToolRegistry
from .verification import Verifier


class AutonomyRuntime:
    """Agent runtime and the only component allowed to activate top-level actions."""

    def __init__(
        self,
        model: CandidateModel,
        tools: ToolRegistry,
        verifier: Verifier,
        store: AutonomyStore,
        selector: CandidateSelector | None = None,
        approval: ApprovalPolicy | None = None,
        recipes: RecipeEngine | None = None,
        procedure_skills: ProcedureSkillLibrary | None = None,
    ):
        self.model = model
        self.tools = tools
        self.verifier = verifier
        self.store = store
        self.selector = selector or CandidateSelector(beam_width=3)
        self.approval = approval or ApprovalPolicy()
        self.recipes = recipes or RecipeEngine(store)
        self.procedure_skills = procedure_skills

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

        state = RunState(
            run_id=uuid.uuid4().hex,
            goal=Goal(goal.strip()),
            conversation_context=conversation_context.strip(),
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

        action, blocked = self.choose_executable_action(ranked)
        if blocked:
            self.store.record_event(
                state.run_id,
                state.step,
                "execution_candidates_blocked",
                blocked,
            )
        if action is None:
            return self.finish_run(
                state,
                TerminationReason.NO_CANDIDATES,
                "no ranked candidate passed execution boundary validation",
            )

        self.store.record_event(
            state.run_id,
            state.step,
            "action_selected",
            {
                "tool": action.tool,
                "arguments": action.arguments,
                "purpose": action.purpose,
                "risk_level": action.risk_level.value,
                "expected_effect": action.expected_effect,
                "verification_plan": action.verification_plan,
                "tool_spec": self.tools.spec(action.tool).summary,
            },
        )
        allowed, approval_reason = self.authorize_action(state, action, interactive=interactive)
        if not allowed:
            return self.finish_run(
                state,
                TerminationReason.APPROVAL_DENIED,
                approval_reason,
            )

        observation = self.execute_action(state, action)
        verification = self.verify_observation(state, action, observation)
        transition = Transition(state.run_id, state.step, action, observation, verification)
        self.store.record_transition(transition)
        state.transitions.append(transition)
        self.learn_from_transition(state, transition)
        state.current_state = verification.reason

        if verification.goal_achieved:
            return self.finish_run(state, TerminationReason.ACHIEVED, verification.reason)
        if not verification.continue_allowed:
            return self.finish_run(state, TerminationReason.BLOCKED, verification.reason)
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
            *self.model.propose(state, self.tools.names, loaded_skills),
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
            if transition.observation.succeeded and transition.verification.verified
        }
        ranked = self.selector.select(
            candidates,
            self.tools.names,
            completed_action_fingerprints,
            self.tools.rejection_reason,
            self._risk_for_intent,
            self._side_effects_for_intent,
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

    def choose_executable_action(
        self,
        ranked: list[CandidatePath],
    ) -> tuple[Action | None, list[dict[str, str]]]:
        blocked: list[dict[str, str]] = []
        for candidate in ranked:
            if not candidate.actions:
                blocked.append(
                    {
                        "candidate_id": candidate.id,
                        "source": candidate.source,
                        "reason": "candidate has no actions",
                    }
                )
                continue
            intent = candidate.next_action
            if intent.tool not in self.tools.names:
                blocked.append(
                    {
                        "candidate_id": candidate.id,
                        "source": candidate.source,
                        "reason": f"tool is unavailable: {intent.tool}",
                    }
                )
                continue
            reason = self.tools.rejection_reason(intent)
            if reason:
                blocked.append(
                    {
                        "candidate_id": candidate.id,
                        "source": candidate.source,
                        "reason": reason,
                    }
                )
                continue
            action = self.tools.action_from_intent(intent)
            return action, blocked
        return None, blocked

    def _risk_for_intent(self, intent: ActionIntent) -> RiskLevel:
        if intent.tool not in self.tools.names:
            return RiskLevel.HIGH
        return self.tools.spec(intent.tool).default_risk

    def _side_effects_for_intent(self, intent: ActionIntent) -> tuple[str, ...]:
        if intent.tool not in self.tools.names:
            return ("unknown-tool",)
        return self.tools.spec(intent.tool).side_effects

    def authorize_action(
        self,
        state: RunState,
        action: Action,
        *,
        interactive: bool,
    ) -> tuple[bool, str]:
        allowed, approval_reason = self.approval.authorize(action, interactive)
        self.store.record_event(
            state.run_id,
            state.step,
            "approval_decision",
            {"allowed": allowed, "reason": approval_reason},
        )
        return allowed, approval_reason

    def execute_action(self, state: RunState, action: Action) -> Observation:
        observation = self.tools.execute(action)
        self.store.record_event(
            state.run_id,
            state.step,
            "observation",
            observation,
        )
        return observation

    def verify_observation(
        self,
        state: RunState,
        action: Action,
        observation: Observation,
    ) -> Verification:
        verification = self.verifier.verify(state, action, observation)
        self.store.record_event(state.run_id, state.step, "verification", verification)
        return verification

    def learn_from_transition(self, state: RunState, transition: Transition) -> None:
        learned = self.recipes.learn(transition)
        if learned:
            self.store.record_event(
                state.run_id,
                state.step,
                "candidate_recipe_learned",
                learned,
            )

    def finish_run(
        self,
        state: RunState,
        termination: TerminationReason,
        reason: str,
    ) -> RunResult:
        if termination == TerminationReason.ACHIEVED:
            self._maybe_create_procedure_skill_candidate(state)
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

    def _maybe_create_procedure_skill_candidate(self, state: RunState) -> None:
        if not self.procedure_skills or len(state.transitions) < 2:
            return
        if not all(
            transition.observation.succeeded and transition.verification.verified
            for transition in state.transitions
        ):
            return
        try:
            draft = self.model.draft_procedure_skill(state)
            candidate = self.procedure_skills.write_candidate(
                draft,
                source_run_id=state.run_id,
                source_workspace=self.procedure_skills.workspace,
            )
            self.store.record_event(
                state.run_id,
                state.step,
                "procedure_skill_candidate_created",
                candidate,
            )
        except Exception as exc:
            self.store.record_event(
                state.run_id,
                state.step,
                "procedure_skill_candidate_error",
                {"error": str(exc)},
            )
