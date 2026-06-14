import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import autonomy
from autonomy import (
    Action,
    ActionRecipe,
    AutonomyStore,
    GoalStatus,
    Observation,
    Outcome,
    RecipeEngine,
    RecipeStatus,
)
from autonomy.models import Goal, RunState, Transition


class AutonomyNativeRecipeTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = AutonomyStore(Path(self.tmpdir.name) / "autonomy.db")
        self.engine = RecipeEngine(self.store, candidate_threshold=2)

    def tearDown(self):
        self.tmpdir.cleanup()

    def transition(self, run_id, step, action):
        return Transition(
            run_id=run_id,
            step=step,
            action=action,
            observation=Observation(action.id, True, output="ok", evidence=("ok",)),
            outcome=Outcome(True, GoalStatus.CONTINUE, "successful outcome"),
        )

    def test_repeated_success_creates_candidate_but_not_active_recipe(self):
        action = Action(
            tool="filesystem.list",
            arguments={"path": "."},
            expected_effect="orient repository",
            verification_plan="confirm listing",
        )
        for run_id, step in (("run-1", 1), ("run-2", 1)):
            self.store.create_run(run_id, "orient")
            transition = self.transition(run_id, step, action)
            self.store.record_transition(transition)
            learned = self.engine.learn(transition)

        self.assertIsNotNone(learned)
        self.assertTrue(learned.created)
        self.assertEqual(learned.recipe.status, RecipeStatus.CANDIDATE)
        state = RunState("run-3", Goal("orient"))
        self.assertEqual(self.engine.candidates_for(state), [])

        self.store.set_recipe_state(learned.recipe.id, status=RecipeStatus.ACTIVE)
        candidates = self.engine.candidates_for(state)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source, "action_skill")
        self.assertEqual(len(candidates[0].actions), 1)
        self.assertEqual(candidates[0].next_action.recipe_id, learned.recipe.id)

    def test_existing_recipe_update_is_not_created(self):
        action = Action(
            tool="filesystem.list",
            arguments={"path": "."},
            expected_effect="orient repository",
            verification_plan="confirm listing",
        )
        learned = None
        for run_id in ("run-1", "run-2", "run-3"):
            self.store.create_run(run_id, "orient")
            transition = self.transition(run_id, 1, action)
            self.store.record_transition(transition)
            learned = self.engine.learn(transition)

        self.assertIsNotNone(learned)
        self.assertFalse(learned.created)
        self.assertEqual(learned.recipe.evidence_count, 3)

    def test_public_api_does_not_export_recipe_graph_types(self):
        self.assertNotIn("EdgeType", autonomy.__all__)
        self.assertNotIn("RecipeEdge", autonomy.__all__)
        self.assertNotIn("SituationRecipeNode", autonomy.__all__)

    def test_disabled_active_recipe_is_not_selectable(self):
        recipe = ActionRecipe(
            "disabled",
            "disabled",
            "always",
            {"tool": "filesystem.list", "arguments": {"path": "."}},
            "none",
            "verify",
            RecipeStatus.ACTIVE,
            enabled=False,
        )
        self.store.upsert_recipe(recipe)

        self.assertEqual(self.engine.candidates_for(RunState("run", Goal("anything"))), [])

    def test_new_database_does_not_create_recipe_graph_tables(self):
        with closing(sqlite3.connect(self.store.db_path)) as conn:
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        self.assertIn("action_recipes", table_names)
        self.assertNotIn("recipe_edges", table_names)
        self.assertNotIn("situation_recipe_nodes", table_names)

    


if __name__ == "__main__":
    unittest.main()
