import tempfile
import unittest
from pathlib import Path

from skill_graph import EdgeStatus, EdgeType, SQLiteSkillGraphStore
from skill_graph.models import SkillGraphEdge, SituationSkillNode


class SkillGraphStoreTest(unittest.TestCase):
    def test_sqlite_store_persists_nodes_edges_and_bayesian_updates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteSkillGraphStore(str(Path(tmpdir) / "skill_graph.db"))
            source = SituationSkillNode(
                node_id="orient",
                situation="unknown codebase",
                skill_name="repo_orientation",
                condition="coding task starts without repository context",
                evidence="initial design",
            )
            target = SituationSkillNode(
                node_id="implement",
                situation="known codebase",
                skill_name="implementation",
                condition="repository structure is understood",
                evidence="initial design",
            )
            edge = SkillGraphEdge(
                edge_id="orient-precedes-implement",
                source_node_id=source.node_id,
                target_node_id=target.node_id,
                edge_type=EdgeType.PRECEDES,
                condition="unknown codebase coding task",
                status=EdgeStatus.ACTIVE,
            )

            store.upsert_node(source)
            store.upsert_node(target)
            store.upsert_edge(edge)

            stored_edge = store.list_edges(source_node_id=source.node_id)[0]
            self.assertEqual(stored_edge.confidence_mean, 0.5)
            self.assertEqual(stored_edge.evidence_count, 0)

            stored_edge = store.record_edge_success(
                edge.edge_id,
                verified_at="2026-06-03T00:00:00Z",
            )
            self.assertEqual(stored_edge.alpha, 2)
            self.assertEqual(stored_edge.beta, 1)
            self.assertEqual(round(stored_edge.confidence_mean, 3), 0.667)
            self.assertEqual(stored_edge.evidence_count, 1)

            stored_edge = store.record_edge_failure(
                edge.edge_id,
                failure_condition="repository already known",
                verified_at="2026-06-04T00:00:00Z",
            )
            self.assertEqual(stored_edge.alpha, 2)
            self.assertEqual(stored_edge.beta, 2)
            self.assertEqual(stored_edge.confidence_mean, 0.5)
            self.assertEqual(stored_edge.evidence_count, 2)
            self.assertEqual(stored_edge.failure_conditions, ["repository already known"])

    def test_store_filters_active_edges(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteSkillGraphStore(str(Path(tmpdir) / "skill_graph.db"))
            node_a = SituationSkillNode("a", "situation a", "skill_a", "condition a")
            node_b = SituationSkillNode("b", "situation b", "skill_b", "condition b")
            store.upsert_node(node_a)
            store.upsert_node(node_b)
            store.upsert_edge(
                SkillGraphEdge(
                    edge_id="active-edge",
                    source_node_id="a",
                    target_node_id="b",
                    edge_type=EdgeType.ENABLES,
                    condition="active condition",
                    status=EdgeStatus.ACTIVE,
                )
            )
            store.upsert_edge(
                SkillGraphEdge(
                    edge_id="proposed-edge",
                    source_node_id="a",
                    target_node_id="b",
                    edge_type=EdgeType.ALTERNATIVE_TO,
                    condition="proposed condition",
                    status=EdgeStatus.PROPOSED,
                )
            )

            active_edges = store.list_edges(statuses=[EdgeStatus.ACTIVE])

            self.assertEqual([edge.edge_id for edge in active_edges], ["active-edge"])
