import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from autonomy import (
    Action,
    ActionRecipe,
    AutonomyStore,
    EdgeType,
    GoalStatus,
    Observation,
    Outcome,
    RecipeEdge,
    RecipeEngine,
    RecipeStatus,
    SituationRecipeNode,
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
        self.assertEqual(learned.status, RecipeStatus.CANDIDATE)
        state = RunState("run-3", Goal("orient"))
        self.assertEqual(self.engine.candidates_for(state), [])

        self.store.set_recipe_state(learned.id, status=RecipeStatus.ACTIVE)
        candidates = self.engine.candidates_for(state)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].next_action.recipe_id, learned.id)

    def test_active_edges_create_paths_and_bayesian_updates(self):
        source = ActionRecipe(
            "source",
            "list",
            "repo exists",
            {"tool": "filesystem.list", "arguments": {"path": "."}},
            "list files",
            "confirm listing",
            RecipeStatus.ACTIVE,
        )
        target = ActionRecipe(
            "target",
            "search",
            "files listed",
            {"tool": "search.text", "arguments": {"path": ".", "query": "TODO"}},
            "find TODOs",
            "confirm search",
            RecipeStatus.ACTIVE,
        )
        source_node = SituationRecipeNode(
            "source-node", "unknown repository", source.id, "repository not inspected", "test"
        )
        target_node = SituationRecipeNode(
            "target-node", "known repository", target.id, "repository inspected", "test"
        )
        edge = RecipeEdge(
            "edge", source_node.id, target_node.id, EdgeType.PRECEDES, "orientation first"
        )
        self.store.upsert_recipe(source)
        self.store.upsert_recipe(target)
        self.store.upsert_recipe_node(source_node)
        self.store.upsert_recipe_node(target_node)
        self.store.upsert_recipe_edge(edge)

        candidates = self.engine.candidates_for(RunState("run", Goal("find TODOs")))
        graph_path = next(item for item in candidates if len(item.actions) == 2)
        self.assertEqual([action.recipe_id for action in graph_path.actions], ["source", "target"])
        self.assertEqual(
            [node.situation for node in self.store.list_recipe_nodes()],
            ["unknown repository", "known repository"],
        )

        self.store.create_run("run", "find TODOs")
        intent = graph_path.next_action
        action = Action(
            intent.tool,
            intent.arguments,
            intent.purpose,
            "agent-derived outcome",
            purpose=intent.purpose,
            recipe_id=intent.recipe_id,
            edge_ids=intent.edge_ids,
        )
        transition = self.transition("run", 1, action)
        self.store.record_transition(transition)
        self.engine.learn(transition)
        self.assertEqual(self.store.list_recipe_edges()[0].alpha, 2)

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

    def test_legacy_skill_tables_migrate_without_deletion(self):
        db_path = Path(self.tmpdir.name) / "legacy.db"
        with closing(sqlite3.connect(db_path)) as conn:
            with conn:
                conn.executescript(
                    """
                CREATE TABLE skills (
                    skill_id TEXT PRIMARY KEY, intent TEXT, preconditions TEXT,
                    action_template_json TEXT, expected_effect TEXT,
                    verification_plan TEXT, status TEXT, enabled INTEGER,
                    evidence_count INTEGER
                );
                CREATE TABLE situation_skill_nodes (
                    node_id TEXT PRIMARY KEY, situation TEXT, skill_id TEXT,
                    condition TEXT, evidence TEXT
                );
                CREATE TABLE skill_edges (
                    edge_id TEXT PRIMARY KEY, source_node_id TEXT, target_node_id TEXT,
                    edge_type TEXT, condition TEXT, alpha INTEGER, beta INTEGER,
                    enabled INTEGER
                );
                INSERT INTO skills VALUES
                    ('legacy', 'intent', 'condition', '{"tool":"filesystem.list"}',
                     'effect', 'verify', 'active', 1, 3);
                INSERT INTO situation_skill_nodes VALUES
                    ('node', 'situation', 'legacy', 'condition', 'evidence');
                INSERT INTO skill_edges VALUES
                    ('edge', 'node', 'node', 'precedes', 'condition', 2, 1, 1);
                    """
                )

        migrated = AutonomyStore(db_path)

        self.assertEqual(migrated.list_recipes()[0].id, "legacy")
        self.assertEqual(migrated.list_recipe_nodes()[0].recipe_id, "legacy")
        self.assertEqual(migrated.list_recipe_edges()[0].alpha, 2)
        with closing(sqlite3.connect(db_path)) as conn:
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'skills'"
                ).fetchone()
            )


if __name__ == "__main__":
    unittest.main()
