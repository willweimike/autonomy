from __future__ import annotations

from dataclasses import replace

from .models import (
    ActionIntent,
    ActionRecipe,
    CandidatePath,
    RecipeStatus,
    RunState,
    Transition,
)
from .store import AutonomyStore


class RecipeEngine:
    """Generate executable candidates from successful single-action recipes."""

    def __init__(self, store: AutonomyStore, candidate_threshold: int = 2):
        self.store = store
        self.candidate_threshold = candidate_threshold

    def candidates_for(self, state: RunState) -> list[CandidatePath]:
        del state
        return [
            CandidatePath(actions=[self._intent_for(recipe)], source="action_skill")
            for recipe in self.store.list_recipes(
                status=RecipeStatus.ACTIVE,
                enabled_only=True,
            )
        ]

    def learn(self, transition: Transition) -> ActionRecipe | None:
        if not transition.outcome.execution_ok or not transition.observation.succeeded:
            return None
        evidence_count = self.store.successful_action_count(transition.action.fingerprint)
        if evidence_count < self.candidate_threshold:
            return None
        recipe_id = f"candidate-{transition.action.fingerprint[:16]}"
        existing = {recipe.id: recipe for recipe in self.store.list_recipes()}
        if recipe_id in existing:
            recipe = replace(existing[recipe_id], evidence_count=evidence_count)
        else:
            recipe = ActionRecipe(
                id=recipe_id,
                intent=transition.action.expected_effect,
                preconditions="Observed in successful outcomes.",
                action_template={
                    "tool": transition.action.tool,
                    "arguments": transition.action.arguments,
                    "purpose": transition.action.purpose or transition.action.expected_effect,
                },
                expected_effect=transition.action.expected_effect,
                verification_plan=transition.action.verification_plan,
                status=RecipeStatus.CANDIDATE,
                enabled=True,
                evidence_count=evidence_count,
            )
        self.store.upsert_recipe(recipe)
        return recipe

    @staticmethod
    def _intent_for(recipe: ActionRecipe) -> ActionIntent:
        template = recipe.action_template
        return ActionIntent(
            tool=str(template["tool"]),
            arguments=dict(template.get("arguments", {})),
            purpose=str(template.get("purpose", recipe.intent)),
            evidence_strength=min(recipe.evidence_count / 10.0, 1.0),
            recipe_id=recipe.id,
        )
