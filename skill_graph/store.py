from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, List, Optional

from .models import EdgeStatus, EdgeType, SkillGraphEdge, SituationSkillNode


class SQLiteSkillGraphStore:
    """SQLite persistence for the long-lived skill experience map."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS situation_skill_nodes (
                    node_id TEXT PRIMARY KEY,
                    situation TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS skill_graph_edges (
                    edge_id TEXT PRIMARY KEY,
                    source_node_id TEXT NOT NULL,
                    target_node_id TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    alpha INTEGER NOT NULL DEFAULT 1,
                    beta INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'proposed',
                    last_verified_at TEXT,
                    failure_conditions_json TEXT NOT NULL DEFAULT '[]',
                    FOREIGN KEY(source_node_id) REFERENCES situation_skill_nodes(node_id),
                    FOREIGN KEY(target_node_id) REFERENCES situation_skill_nodes(node_id)
                )
                """
            )

    def upsert_node(self, node: SituationSkillNode) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO situation_skill_nodes
                    (node_id, situation, skill_name, condition, evidence, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    situation=excluded.situation,
                    skill_name=excluded.skill_name,
                    condition=excluded.condition,
                    evidence=excluded.evidence,
                    metadata_json=excluded.metadata_json
                """,
                (
                    node.node_id,
                    node.situation,
                    node.skill_name,
                    node.condition,
                    node.evidence,
                    json.dumps(node.metadata, sort_keys=True),
                ),
            )

    def upsert_edge(self, edge: SkillGraphEdge) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_graph_edges
                    (
                        edge_id, source_node_id, target_node_id, edge_type,
                        condition, evidence, alpha, beta, status,
                        last_verified_at, failure_conditions_json
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO UPDATE SET
                    source_node_id=excluded.source_node_id,
                    target_node_id=excluded.target_node_id,
                    edge_type=excluded.edge_type,
                    condition=excluded.condition,
                    evidence=excluded.evidence,
                    alpha=excluded.alpha,
                    beta=excluded.beta,
                    status=excluded.status,
                    last_verified_at=excluded.last_verified_at,
                    failure_conditions_json=excluded.failure_conditions_json
                """,
                (
                    edge.edge_id,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.edge_type.value,
                    edge.condition,
                    edge.evidence,
                    edge.alpha,
                    edge.beta,
                    edge.status.value,
                    edge.last_verified_at,
                    json.dumps(edge.failure_conditions),
                ),
            )

    def get_node(self, node_id: str) -> Optional[SituationSkillNode]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM situation_skill_nodes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
        return self._node_from_row(row) if row else None

    def list_nodes(self) -> List[SituationSkillNode]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM situation_skill_nodes ORDER BY node_id"
            ).fetchall()
        return [self._node_from_row(row) for row in rows]

    def list_edges(
        self,
        source_node_id: Optional[str] = None,
        statuses: Optional[Iterable[EdgeStatus]] = None,
    ) -> List[SkillGraphEdge]:
        clauses = []
        params = []
        if source_node_id:
            clauses.append("source_node_id = ?")
            params.append(source_node_id)
        if statuses:
            status_values = [status.value for status in statuses]
            clauses.append(
                "status IN ({})".format(",".join("?" for _ in status_values))
            )
            params.extend(status_values)
        sql = "SELECT * FROM skill_graph_edges"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY edge_id"
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._edge_from_row(row) for row in rows]

    def record_edge_success(self, edge_id: str, verified_at: Optional[str] = None) -> SkillGraphEdge:
        edge = self._require_edge(edge_id)
        edge.record_success(verified_at=verified_at)
        self.upsert_edge(edge)
        return edge

    def record_edge_failure(
        self,
        edge_id: str,
        failure_condition: Optional[str] = None,
        verified_at: Optional[str] = None,
    ) -> SkillGraphEdge:
        edge = self._require_edge(edge_id)
        edge.record_failure(failure_condition=failure_condition, verified_at=verified_at)
        self.upsert_edge(edge)
        return edge

    def _require_edge(self, edge_id: str) -> SkillGraphEdge:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_graph_edges WHERE edge_id = ?",
                (edge_id,),
            ).fetchone()
        if not row:
            raise KeyError("Unknown edge_id: {}".format(edge_id))
        return self._edge_from_row(row)

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> SituationSkillNode:
        return SituationSkillNode(
            node_id=row["node_id"],
            situation=row["situation"],
            skill_name=row["skill_name"],
            condition=row["condition"],
            evidence=row["evidence"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    @staticmethod
    def _edge_from_row(row: sqlite3.Row) -> SkillGraphEdge:
        return SkillGraphEdge(
            edge_id=row["edge_id"],
            source_node_id=row["source_node_id"],
            target_node_id=row["target_node_id"],
            edge_type=EdgeType(row["edge_type"]),
            condition=row["condition"],
            evidence=row["evidence"],
            alpha=int(row["alpha"]),
            beta=int(row["beta"]),
            status=EdgeStatus(row["status"]),
            last_verified_at=row["last_verified_at"],
            failure_conditions=json.loads(row["failure_conditions_json"] or "[]"),
        )
