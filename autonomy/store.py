from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import (
    ActionRecipe,
    ConversationTurn,
    LearningProposal,
    LearningProposalStatus,
    LearningProposalType,
    ProcedureSkillSummary,
    RecipeStatus,
    RunResult,
    Transition,
    jsonable,
)


def format_memory_context(heading: str, memories: list[dict[str, Any]]) -> str:
    if not memories:
        return ""
    lines = [heading]
    for memory in memories:
        content = str(memory["content"]).strip()
        if len(content) > 300:
            content = content[:300].rstrip() + "..."
        lines.append(
            f"- [{memory['scope']}/{memory['wing']}/{memory['room']}] "
            f"{content} (id={memory['id']})"
        )
    return "\n".join(lines)


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
                CREATE TABLE IF NOT EXISTS learning_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    proposal_type TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS curator_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    source_skill TEXT NOT NULL,
                    target_skill TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    wing TEXT NOT NULL,
                    room TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            
    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            is not None
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
                    json.dumps(jsonable(transition.outcome), sort_keys=True),
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

    def delete_procedure_skill_record(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM procedure_skills WHERE name = ?", (name,))

    def record_learning_proposal(self, proposal: LearningProposal) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO learning_proposals
                    (proposal_id, proposal_type, source_run_id, status, reason, confidence, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    proposal_type=excluded.proposal_type,
                    source_run_id=excluded.source_run_id,
                    status=excluded.status,
                    reason=excluded.reason,
                    confidence=excluded.confidence,
                    payload_json=excluded.payload_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    proposal.id,
                    proposal.proposal_type.value,
                    proposal.source_run_id,
                    proposal.status.value,
                    proposal.reason,
                    proposal.confidence,
                    json.dumps(jsonable(proposal.payload), sort_keys=True),
                ),
            )

    def list_learning_proposals(
        self,
        *,
        status: LearningProposalStatus | None = None,
    ) -> list[LearningProposal]:
        sql = "SELECT * FROM learning_proposals"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status.value)
        sql += " ORDER BY created_at, proposal_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            LearningProposal(
                id=row["proposal_id"],
                proposal_type=LearningProposalType(row["proposal_type"]),
                source_run_id=row["source_run_id"],
                status=LearningProposalStatus(row["status"]),
                reason=row["reason"],
                confidence=float(row["confidence"]),
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def set_learning_proposal_status(
        self,
        proposal_id: str,
        status: LearningProposalStatus,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE learning_proposals
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE proposal_id = ?
                """,
                (status.value, proposal_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown learning proposal: {proposal_id}")

    def record_curator_event(
        self,
        event_type: str,
        *,
        source_skill: str = "",
        target_skill: str = "",
        reason: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO curator_events
                    (event_type, source_skill, target_skill, reason, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    source_skill,
                    target_skill,
                    reason,
                    json.dumps(jsonable(payload or {}), sort_keys=True),
                ),
            )

    def list_curator_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM curator_events ORDER BY event_id DESC"
        params: list[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    def create_memory(
        self,
        *,
        scope: str,
        wing: str,
        room: str,
        content: str,
        source_run_id: str = "",
    ) -> dict[str, Any]:
        memory_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memories
                    (id, scope, wing, room, content, source_run_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (memory_id, scope, wing, room, content, source_run_id),
            )
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise RuntimeError("memory insert did not persist")
        return dict(row)

    def list_memories(
        self,
        *,
        scope: str | None = None,
        wing: str | None = None,
        room: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if wing:
            clauses.append("wing = ?")
            params.append(wing)
        if room:
            clauses.append("room = ?")
            params.append(room)
        sql = "SELECT * FROM memories"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def search_memories(
        self,
        query: str,
        *,
        scope: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not terms:
            return []
        rows = self.list_memories(scope=scope, limit=500)

        def score(memory: dict[str, Any]) -> int:
            haystack = " ".join(
                str(memory[field]).casefold()
                for field in ("content", "wing", "room", "scope")
            )
            return sum(1 for term in terms if term in haystack)

        ranked = [
            (score(memory), memory)
            for memory in rows
        ]
        ranked = [item for item in ranked if item[0] > 0]
        ranked.sort(key=lambda item: (-item[0], item[1]["updated_at"], item[1]["id"]))
        return [memory for _, memory in ranked[:limit]]

    def forget_memory(self, memory_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return cursor.rowcount > 0

    def successful_action_count(self, fingerprint: str) -> int:
        count = 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT action_json, observation_json, verification_json FROM transitions"
            ).fetchall()
        for row in rows:
            action = json.loads(row["action_json"])
            observation = json.loads(row["observation_json"])
            outcome = json.loads(row["verification_json"])
            value = json.dumps(
                {"tool": action["tool"], "arguments": action["arguments"]},
                sort_keys=True,
                separators=(",", ":"),
            )
            import hashlib

            if (
                hashlib.sha256(value.encode("utf-8")).hexdigest() == fingerprint
                and observation["succeeded"]
                and outcome["execution_ok"]
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
