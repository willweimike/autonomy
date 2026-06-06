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
    """Generate executable candidates from successful action recipes and graph paths."""

    def __init__(self, store: AutonomyStore, candidate_threshold: int = 2):
        self.store = store
        self.candidate_threshold = candidate_threshold

    def candidates_for(self, state: RunState) -> list[CandidatePath]:
        del state
        candidates: list[CandidatePath] = []
        active_recipes = {
            recipe.id: recipe
            for recipe in self.store.list_recipes(
                status=RecipeStatus.ACTIVE,
                enabled_only=True,
            )
        }
        nodes = {node.id: node for node in self.store.list_recipe_nodes()}
        for recipe in active_recipes.values():
            candidates.append(
                CandidatePath(actions=[self._intent_for(recipe)], source="recipe_graph")
            )
        for edge in self.store.list_recipe_edges():
            if (
                not edge.enabled
                or edge.source_node_id not in nodes
                or edge.target_node_id not in nodes
            ):
                continue
            source_recipe_id = nodes[edge.source_node_id].recipe_id
            target_recipe_id = nodes[edge.target_node_id].recipe_id
            if source_recipe_id not in active_recipes or target_recipe_id not in active_recipes:
                continue
            candidates.append(
                CandidatePath(
                    actions=[
                            self._intent_for(
                                active_recipes[source_recipe_id],
                                edge_confidence=edge.reliability,
                                edge_ids=(edge.id,),
                            ),
                        self._intent_for(
                            active_recipes[target_recipe_id],
                            edge_confidence=edge.reliability,
                        ),
                    ],
                    source="recipe_graph",
                )
            )
        return candidates

    def learn(self, transition: Transition) -> ActionRecipe | None:
        self.store.update_recipe_edges(
            transition.action.edge_ids,
            success=transition.outcome.execution_ok and transition.observation.succeeded,
        )
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
    def _intent_for(
        recipe: ActionRecipe,
        edge_confidence: float | None = None,
        edge_ids: tuple[str, ...] = (),
    ) -> ActionIntent:
        template = recipe.action_template
        return ActionIntent(
            tool=str(template["tool"]),
            arguments=dict(template.get("arguments", {})),
            purpose=str(template.get("purpose", recipe.intent)),
            edge_confidence=(
                edge_confidence
                if edge_confidence is not None
                else float(template.get("edge_confidence", 0.5))
            ),
            evidence_strength=min(recipe.evidence_count / 10.0, 1.0),
            recipe_id=recipe.id,
            edge_ids=edge_ids,
        )
