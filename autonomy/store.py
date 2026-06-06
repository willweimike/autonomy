from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import (
    ActionRecipe,
    ConversationTurn,
    EdgeType,
    ProcedureSkillSummary,
    RecipeEdge,
    RecipeStatus,
    RunResult,
    SituationRecipeNode,
    Transition,
    jsonable,
)


class AutonomyStore:
    """Single persistence boundary for journals, recipes, and skill metadata."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    termination TEXT,
                    reason TEXT NOT NULL DEFAULT '',
                    steps_executed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS transitions (
                    run_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    action_json TEXT NOT NULL,
                    observation_json TEXT NOT NULL,
                    verification_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, step),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS action_recipes (
                    recipe_id TEXT PRIMARY KEY,
                    intent TEXT NOT NULL,
                    preconditions TEXT NOT NULL,
                    action_template_json TEXT NOT NULL,
                    expected_effect TEXT NOT NULL,
                    verification_plan TEXT NOT NULL,
                    status TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    evidence_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS recipe_edges (
                    edge_id TEXT PRIMARY KEY,
                    source_node_id TEXT NOT NULL,
                    target_node_id TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    alpha INTEGER NOT NULL DEFAULT 1,
                    beta INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS situation_recipe_nodes (
                    node_id TEXT PRIMARY KEY,
                    situation TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS procedure_skills (
                    name TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    version TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    platforms_json TEXT NOT NULL,
                    requires_tools_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    path TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    load_count INTEGER NOT NULL DEFAULT 0,
                    last_loaded_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                    session_id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    run_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(session_id) REFERENCES conversation_sessions(session_id)
                );
                """
            )
            self._migrate_legacy_skill_tables(conn)

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            is not None
        )

    def _migrate_legacy_skill_tables(self, conn: sqlite3.Connection) -> None:
        if self._table_exists(conn, "skills"):
            conn.execute(
                """
                INSERT OR IGNORE INTO action_recipes
                SELECT skill_id, intent, preconditions, action_template_json,
                       expected_effect, verification_plan, status, enabled, evidence_count
                FROM skills
                """
            )
        if self._table_exists(conn, "situation_skill_nodes"):
            conn.execute(
                """
                INSERT OR IGNORE INTO situation_recipe_nodes
                SELECT node_id, situation, skill_id, condition, evidence
                FROM situation_skill_nodes
                """
            )
        if self._table_exists(conn, "skill_edges"):
            conn.execute(
                """
                INSERT OR IGNORE INTO recipe_edges
                SELECT edge_id, source_node_id, target_node_id, edge_type,
                       condition, alpha, beta, enabled
                FROM skill_edges
                """
            )

    def create_run(self, run_id: str, goal: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO runs (run_id, goal) VALUES (?, ?)", (run_id, goal))

    def record_event(self, run_id: str, step: int, event_type: str, payload: Any) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO run_events (run_id, step, event_type, payload_json) VALUES (?, ?, ?, ?)",
                (run_id, step, event_type, json.dumps(jsonable(payload), sort_keys=True)),
            )

    def record_transition(self, transition: Transition) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO transitions
                    (run_id, step, action_json, observation_json, verification_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    transition.run_id,
                    transition.step,
                    json.dumps(jsonable(transition.action), sort_keys=True),
                    json.dumps(jsonable(transition.observation), sort_keys=True),
                    json.dumps(jsonable(transition.verification), sort_keys=True),
                ),
            )

    def complete_run(self, result: RunResult) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET termination = ?, reason = ?, steps_executed = ?, completed_at = CURRENT_TIMESTAMP
                WHERE run_id = ?
                """,
                (result.termination.value, result.reason, result.steps_executed, result.run_id),
            )

    def inspect_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                raise KeyError(f"unknown run: {run_id}")
            events = conn.execute(
                "SELECT step, event_type, payload_json, created_at FROM run_events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return {
            "run": dict(run),
            "events": [
                {**dict(event), "payload": json.loads(event["payload_json"])}
                for event in events
            ],
        }

    def create_conversation_session(self, session_id: str, workspace: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_sessions (session_id, workspace)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    workspace=excluded.workspace,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (session_id, workspace),
            )

    def update_conversation_workspace(self, session_id: str, workspace: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE conversation_sessions
                SET workspace = ?, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                (workspace, session_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown conversation session: {session_id}")

    def record_conversation_turn(self, turn: ConversationTurn) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversation_turns
                    (turn_id, session_id, role, content, run_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    turn.id,
                    turn.session_id,
                    turn.role,
                    turn.content,
                    turn.run_id,
                    json.dumps(jsonable(turn.metadata), sort_keys=True),
                ),
            )
            conn.execute(
                """
                UPDATE conversation_sessions
                SET updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                (turn.session_id,),
            )

    def link_conversation_turn_run(self, turn_id: str, run_id: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE conversation_turns SET run_id = ? WHERE turn_id = ?",
                (run_id, turn_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown conversation turn: {turn_id}")

    def list_conversation_turns(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[ConversationTurn]:
        order = "DESC" if limit is not None else "ASC"
        sql = f"""
            SELECT *
            FROM conversation_turns
            WHERE session_id = ?
            ORDER BY rowid {order}
        """
        params: list[Any] = [session_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        turns = [self._conversation_turn_from_row(row) for row in rows]
        if limit is not None:
            turns.reverse()
        return turns

    def inspect_conversation(self, session_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            session = conn.execute(
                "SELECT * FROM conversation_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise KeyError(f"unknown conversation session: {session_id}")
            rows = conn.execute(
                """
                SELECT *
                FROM conversation_turns
                WHERE session_id = ?
                ORDER BY rowid
                """,
                (session_id,),
            ).fetchall()
        return {
            "session": dict(session),
            "turns": [jsonable(self._conversation_turn_from_row(row)) for row in rows],
        }

    def upsert_recipe(self, recipe: ActionRecipe) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO action_recipes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recipe_id) DO UPDATE SET
                    intent=excluded.intent,
                    preconditions=excluded.preconditions,
                    action_template_json=excluded.action_template_json,
                    expected_effect=excluded.expected_effect,
                    verification_plan=excluded.verification_plan,
                    status=excluded.status,
                    enabled=excluded.enabled,
                    evidence_count=excluded.evidence_count
                """,
                (
                    recipe.id,
                    recipe.intent,
                    recipe.preconditions,
                    json.dumps(recipe.action_template, sort_keys=True),
                    recipe.expected_effect,
                    recipe.verification_plan,
                    recipe.status.value,
                    int(recipe.enabled),
                    recipe.evidence_count,
                ),
            )

    def list_recipes(
        self,
        status: RecipeStatus | None = None,
        enabled_only: bool = False,
    ) -> list[ActionRecipe]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status.value)
        if enabled_only:
            clauses.append("enabled = 1")
        sql = "SELECT * FROM action_recipes"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY recipe_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._recipe_from_row(row) for row in rows]

    def set_recipe_state(
        self,
        recipe_id: str,
        *,
        status: RecipeStatus | None = None,
        enabled: bool | None = None,
    ) -> None:
        assignments: list[str] = []
        params: list[Any] = []
        if status is not None:
            assignments.append("status = ?")
            params.append(status.value)
        if enabled is not None:
            assignments.append("enabled = ?")
            params.append(int(enabled))
        if not assignments:
            return
        params.append(recipe_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE action_recipes SET {', '.join(assignments)} WHERE recipe_id = ?",
                params,
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown recipe: {recipe_id}")

    def upsert_recipe_edge(self, edge: RecipeEdge) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recipe_edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO UPDATE SET
                    source_node_id=excluded.source_node_id,
                    target_node_id=excluded.target_node_id,
                    edge_type=excluded.edge_type,
                    condition=excluded.condition,
                    alpha=excluded.alpha,
                    beta=excluded.beta,
                    enabled=excluded.enabled
                """,
                (
                    edge.id,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.edge_type.value,
                    edge.condition,
                    edge.alpha,
                    edge.beta,
                    int(edge.enabled),
                ),
            )

    def upsert_recipe_node(self, node: SituationRecipeNode) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO situation_recipe_nodes VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    situation=excluded.situation,
                    recipe_id=excluded.recipe_id,
                    condition=excluded.condition,
                    evidence=excluded.evidence
                """,
                (node.id, node.situation, node.recipe_id, node.condition, node.evidence),
            )

    def list_recipe_nodes(self) -> list[SituationRecipeNode]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM situation_recipe_nodes ORDER BY node_id"
            ).fetchall()
        return [
            SituationRecipeNode(
                id=row["node_id"],
                situation=row["situation"],
                recipe_id=row["recipe_id"],
                condition=row["condition"],
                evidence=row["evidence"],
            )
            for row in rows
        ]

    def update_recipe_edges(self, edge_ids: tuple[str, ...], success: bool) -> None:
        if not edge_ids:
            return
        column = "alpha" if success else "beta"
        with self._connect() as conn:
            for edge_id in edge_ids:
                conn.execute(
                    f"UPDATE recipe_edges SET {column} = {column} + 1 WHERE edge_id = ?",
                    (edge_id,),
                )

    def list_recipe_edges(self) -> list[RecipeEdge]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM recipe_edges ORDER BY edge_id").fetchall()
        return [
            RecipeEdge(
                id=row["edge_id"],
                source_node_id=row["source_node_id"],
                target_node_id=row["target_node_id"],
                edge_type=EdgeType(row["edge_type"]),
                condition=row["condition"],
                alpha=int(row["alpha"]),
                beta=int(row["beta"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def sync_procedure_skill(self, skill: ProcedureSkillSummary) -> ProcedureSkillSummary:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO procedure_skills
                    (name, description, version, tags_json, platforms_json,
                     requires_tools_json, source, path, file_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description,
                    version=excluded.version,
                    tags_json=excluded.tags_json,
                    platforms_json=excluded.platforms_json,
                    requires_tools_json=excluded.requires_tools_json,
                    source=excluded.source,
                    path=excluded.path,
                    file_hash=excluded.file_hash
                """,
                (
                    skill.name,
                    skill.description,
                    skill.version,
                    json.dumps(skill.tags),
                    json.dumps(skill.platforms),
                    json.dumps(skill.requires_tools),
                    skill.source,
                    skill.path,
                    skill.file_hash,
                ),
            )
            row = conn.execute(
                "SELECT enabled FROM procedure_skills WHERE name = ?",
                (skill.name,),
            ).fetchone()
        return ProcedureSkillSummary(
            **{**skill.__dict__, "enabled": bool(row["enabled"])},
        )

    def set_procedure_skill_enabled(self, name: str, enabled: bool) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE procedure_skills SET enabled = ? WHERE name = ?",
                (int(enabled), name),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown procedure skill: {name}")

    def record_procedure_skill_loaded(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE procedure_skills
                SET load_count = load_count + 1, last_loaded_at = CURRENT_TIMESTAMP
                WHERE name = ?
                """,
                (name,),
            )

    def list_procedure_skill_records(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM procedure_skills ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    def successful_action_count(self, fingerprint: str) -> int:
        count = 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT action_json, observation_json, verification_json FROM transitions"
            ).fetchall()
        for row in rows:
            action = json.loads(row["action_json"])
            observation = json.loads(row["observation_json"])
            verification = json.loads(row["verification_json"])
            value = json.dumps(
                {"tool": action["tool"], "arguments": action["arguments"]},
                sort_keys=True,
                separators=(",", ":"),
            )
            import hashlib

            if (
                hashlib.sha256(value.encode("utf-8")).hexdigest() == fingerprint
                and observation["succeeded"]
                and verification["verified"]
            ):
                count += 1
        return count

    @staticmethod
    def _recipe_from_row(row: sqlite3.Row) -> ActionRecipe:
        return ActionRecipe(
            id=row["recipe_id"],
            intent=row["intent"],
            preconditions=row["preconditions"],
            action_template=json.loads(row["action_template_json"]),
            expected_effect=row["expected_effect"],
            verification_plan=row["verification_plan"],
            status=RecipeStatus(row["status"]),
            enabled=bool(row["enabled"]),
            evidence_count=int(row["evidence_count"]),
        )

    @staticmethod
    def _conversation_turn_from_row(row: sqlite3.Row) -> ConversationTurn:
        return ConversationTurn(
            id=row["turn_id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            run_id=row["run_id"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
        )
