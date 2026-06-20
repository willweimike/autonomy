from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import yaml
import sqlglot
from sqlglot import exp

from ...models import Observation, RiskLevel
from ...providers import ModelClientError, ModelConfigStore, ProviderConfigurationError, create_provider
from ...storage import workspace_autonomy_home
from ..registry import ToolRegistry

DEFAULT_MAX_ROWS = 100
HARD_MAX_ROWS = 1000
DEFAULT_SOURCE_DIALECT = "postgres"


class DatabaseRetrievalError(ValueError):
    pass


def _connections_path(root: Path) -> Path:
    return workspace_autonomy_home(root) / "database_connections.yaml"


def _load_connections(root: Path) -> dict[str, dict[str, Any]]:
    path = _connections_path(root)
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    connections = data.get("connections", data) if isinstance(data, dict) else None
    if not isinstance(connections, dict):
        raise DatabaseRetrievalError("database_connections.yaml must contain a mapping")
    return {
        str(name): config
        for name, config in connections.items()
        if isinstance(config, dict)
    }


def _connection(root: Path, database_id: str) -> dict[str, Any]:
    connections = _load_connections(root)
    if database_id not in connections:
        raise DatabaseRetrievalError(f"unknown database_id: {database_id}")
    config = connections[database_id]
    dialect = str(config.get("dialect", "sqlite")).strip().lower()
    if dialect not in sqlglot.Dialects:
        raise DatabaseRetrievalError(f"unsupported SQL dialect: {dialect}")
    if dialect == "sqlite":
        raw_path = str(config.get("path", "")).strip()
        if not raw_path:
            raise DatabaseRetrievalError(f"sqlite database '{database_id}' is missing path")
        db_path = (
            (root / raw_path).resolve()
            if not Path(raw_path).is_absolute()
            else Path(raw_path).resolve()
        )
        if db_path != root and root not in db_path.parents:
            raise DatabaseRetrievalError("database path escapes workspace")
        return {**config, "dialect": dialect, "path": str(db_path)}
    return {**config, "dialect": dialect}


def _allowed_tables(config: dict[str, Any]) -> set[str] | None:
    raw = config.get("allowed_tables") or config.get("allow_tables")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise DatabaseRetrievalError("allowed_tables must be an array")
    return {str(table).lower() for table in raw}


def _normalize_max_rows(value: Any) -> int:
    return max(1, min(int(value or DEFAULT_MAX_ROWS), HARD_MAX_ROWS))


def _normalize_dialect(value: Any, *, default: str) -> str:
    dialect = str(value or default).strip().lower()
    if dialect not in sqlglot.Dialects:
        raise DatabaseRetrievalError(f"unsupported SQL dialect: {dialect}")
    return dialect


def _parse_single_statement(sql: str, *, source_dialect: str):
    if not str(sql).strip():
        raise DatabaseRetrievalError("sql must not be empty")
    statements = [stmt for stmt in sqlglot.parse(sql, read=source_dialect) if stmt is not None]
    if len(statements) != 1:
        raise DatabaseRetrievalError("exactly one SQL statement is allowed")
    return statements[0]


def _mutation_expression_types() -> tuple[type, ...]:
    names = (
        "Insert",
        "Update",
        "Delete",
        "Drop",
        "Create",
        "Alter",
        "TruncateTable",
        "Merge",
        "Replace",
        "Command",
        "Copy",
        "Grant",
        "Revoke",
        "Execute",
    )
    return tuple(getattr(exp, name) for name in names if hasattr(exp, name))


def _read_only_root(expression) -> bool:
    return isinstance(expression, (exp.Select, exp.Union, exp.Intersect, exp.Except))


def _cte_aliases(expression) -> set[str]:
    return {
        str(cte.alias).lower()
        for cte in expression.find_all(exp.CTE)
        if cte.alias
    }


def _referenced_tables(expression) -> tuple[str, ...]:
    ctes = _cte_aliases(expression)
    return tuple(
        sorted(
            {
                table.name.lower()
                for table in expression.find_all(exp.Table)
                if table.name and table.name.lower() not in ctes
            }
        )
    )


def _referenced_columns(expression) -> tuple[str, ...]:
    columns = set()
    for column in expression.find_all(exp.Column):
        name = column.name.lower() if column.name else ""
        if not name:
            continue
        table = column.table.lower() if column.table else ""
        columns.add(f"{table}.{name}" if table else name)
    return tuple(sorted(columns))


def _literal_int(node) -> int | None:
    expression = getattr(node, "expression", None)
    if expression is None:
        return None
    try:
        return int(expression.name)
    except (TypeError, ValueError):
        return None


def _enforce_limit(expression, max_rows: int):
    existing = _literal_int(expression.args.get("limit"))
    if existing is None or existing > max_rows:
        return expression.limit(max_rows, copy=True)
    return expression


def _ensure_read_only_select(
    sql: str,
    *,
    source_dialect: str = DEFAULT_SOURCE_DIALECT,
    target_dialect: str = "sqlite",
    allowed_tables: set[str] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> dict[str, Any]:
    source = _normalize_dialect(source_dialect, default=DEFAULT_SOURCE_DIALECT)
    target = _normalize_dialect(target_dialect, default="sqlite")
    expression = _parse_single_statement(sql, source_dialect=source)
    if not _read_only_root(expression):
        raise DatabaseRetrievalError(
            f"Only read-only SELECT queries are allowed, got {expression.key.upper()}"
        )
    for node in expression.walk():
        if isinstance(node, _mutation_expression_types()):
            raise DatabaseRetrievalError(
                f"blocked non-read-only SQL expression: {node.key.upper()}"
            )
    tables = _referenced_tables(expression)
    if allowed_tables is not None:
        unknown = sorted(set(tables) - allowed_tables)
        if unknown:
            raise DatabaseRetrievalError(
                "query references tables outside allowed_tables: " + ", ".join(unknown)
            )
    limited = _enforce_limit(expression, _normalize_max_rows(max_rows))
    return {
        "sql": limited.sql(dialect=target),
        "source_dialect": source,
        "target_dialect": target,
        "referenced_tables": list(tables),
        "referenced_columns": list(_referenced_columns(expression)),
    }


_SQLITE_READ_ONLY_ACTIONS = {
    action
    for action in (
        getattr(sqlite3, "SQLITE_SELECT", None),
        getattr(sqlite3, "SQLITE_READ", None),
        getattr(sqlite3, "SQLITE_FUNCTION", None),
        getattr(sqlite3, "SQLITE_RECURSIVE", None),
    )
    if action is not None
}


def _sqlite_read_only_authorizer(allowed_tables: set[str] | None):
    def authorize(action, arg1, _arg2, _database, _trigger) -> int:
        if action not in _SQLITE_READ_ONLY_ACTIONS:
            return sqlite3.SQLITE_DENY
        if (
            allowed_tables is not None
            and action == sqlite3.SQLITE_READ
            and str(arg1 or "").lower() not in allowed_tables
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _connect_read_only(
    db_path: str,
    *,
    allowed_tables: set[str] | None = None,
) -> sqlite3.Connection:
    uri = f"file:{Path(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(_sqlite_read_only_authorizer(allowed_tables))
    return conn


def _schema(config: dict[str, Any], database_id: str) -> dict[str, Any]:
    if config["dialect"] != "sqlite":
        configured = _configured_schema(config, database_id)
        if configured is None:
            raise DatabaseRetrievalError(
                f"{config['dialect']} connections require configured schema metadata"
            )
        return configured
    allowed = _allowed_tables(config)
    uri = f"file:{Path(config['path'])}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables = []
        for row in rows:
            table_name = str(row["name"])
            if allowed is not None and table_name.lower() not in allowed:
                continue
            columns = [
                {
                    "name": str(column["name"]),
                    "type": str(column["type"] or ""),
                    "nullable": not bool(column["notnull"]),
                    "primary_key": bool(column["pk"]),
                }
                for column in conn.execute(
                    f"PRAGMA table_info({_quote_identifier(table_name)})"
                ).fetchall()
            ]
            tables.append({"name": table_name, "columns": columns})
    return {
        "success": True,
        "database_id": database_id,
        "dialect": config["dialect"],
        "read_only": True,
        "tables": tables,
    }


def _configured_schema(config: dict[str, Any], database_id: str) -> dict[str, Any] | None:
    raw = config.get("schema", {}).get("tables") if isinstance(config.get("schema"), dict) else None
    raw = raw or config.get("tables")
    if raw is None:
        return None
    tables = []
    if isinstance(raw, dict):
        iterator = raw.items()
    elif isinstance(raw, list):
        iterator = ((item.get("name"), item) for item in raw if isinstance(item, dict))
    else:
        raise DatabaseRetrievalError("configured schema tables must be an object or array")
    allowed = _allowed_tables(config)
    for name, details in iterator:
        if not name:
            continue
        table_name = str(name)
        if allowed is not None and table_name.lower() not in allowed:
            continue
        columns_raw = details.get("columns", details) if isinstance(details, dict) else details
        columns = []
        if isinstance(columns_raw, dict):
            for column_name, column_type in columns_raw.items():
                columns.append({"name": str(column_name), "type": str(column_type)})
        elif isinstance(columns_raw, list):
            for column in columns_raw:
                if isinstance(column, dict):
                    columns.append(
                        {
                            "name": str(column.get("name", "")),
                            "type": str(column.get("type", "")),
                        }
                    )
                else:
                    columns.append({"name": str(column), "type": ""})
        tables.append({"name": table_name, "columns": columns})
    return {
        "success": True,
        "database_id": database_id,
        "dialect": config["dialect"],
        "read_only": True,
        "tables": tables,
    }


def _sqlite_table_names(db_path: str) -> set[str]:
    uri = f"file:{Path(db_path)}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {str(row[0]).lower() for row in rows}


def _reject_disallowed_tables(config: dict[str, Any], sql: str) -> None:
    allowed = _allowed_tables(config)
    if allowed is None:
        return
    for table in sorted(_sqlite_table_names(config["path"]) - allowed):
        if table and re.search(rf"\b{re.escape(table)}\b", sql, re.IGNORECASE):
            raise DatabaseRetrievalError(f"access to {table} is not allowed")


def _query(config: dict[str, Any], database_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if config["dialect"] != "sqlite":
        raise DatabaseRetrievalError(f"execution for dialect '{config['dialect']}' is not implemented")
    validation = _validate_sql(config, database_id, arguments)
    sql = validation["sql"]
    _reject_disallowed_tables(config, sql)
    max_rows = _normalize_max_rows(arguments.get("max_rows"))
    wrapped_sql = f"SELECT * FROM ({sql}) LIMIT ?"
    try:
        with _connect_read_only(
            config["path"],
            allowed_tables=_allowed_tables(config),
        ) as conn:
            rows = [dict(row) for row in conn.execute(wrapped_sql, (max_rows,)).fetchall()]
    except sqlite3.DatabaseError as exc:
        if "not authorized" in str(exc).lower():
            raise DatabaseRetrievalError("query reads outside allowed_tables") from exc
        raise
    return {
        "success": True,
        "database_id": database_id,
        "dialect": "sqlite",
        "read_only": True,
        "executed": True,
        "sql": sql,
        "source_dialect": validation["source_dialect"],
        "target_dialect": validation["target_dialect"],
        "referenced_tables": validation["referenced_tables"],
        "referenced_columns": validation["referenced_columns"],
        "row_count": len(rows),
        "max_rows": max_rows,
        "rows": rows,
    }


def _explain(config: dict[str, Any], database_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if config["dialect"] != "sqlite":
        raise DatabaseRetrievalError(f"explain for dialect '{config['dialect']}' is not implemented")
    validation = _validate_sql(config, database_id, arguments)
    sql = validation["sql"]
    _reject_disallowed_tables(config, sql)
    try:
        with _connect_read_only(
            config["path"],
            allowed_tables=_allowed_tables(config),
        ) as conn:
            rows = [
                dict(row)
                for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
            ]
    except sqlite3.DatabaseError as exc:
        if "not authorized" in str(exc).lower():
            raise DatabaseRetrievalError("query reads outside allowed_tables") from exc
        raise
    return {
        "success": True,
        "action": "explain",
        "database_id": database_id,
        "dialect": "sqlite",
        "read_only": True,
        "executed": False,
        "sql": sql,
        "source_dialect": validation["source_dialect"],
        "target_dialect": validation["target_dialect"],
        "referenced_tables": validation["referenced_tables"],
        "referenced_columns": validation["referenced_columns"],
        "plan": rows,
    }


def _redact_connection(config: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(config)
    redacted["path"] = "[workspace path]"
    return redacted


def _schema_context(schema: dict[str, Any]) -> str:
    lines = [
        f"Database ID: {schema['database_id']}",
        f"Dialect: {schema['dialect']}",
        "Access policy: read-only SELECT queries only. Use only listed tables and columns.",
        "Tables:",
    ]
    for table in schema["tables"]:
        columns = ", ".join(
            f"{column['name']} {column.get('type', '')}".strip()
            for column in table.get("columns", [])
            if column.get("name")
        )
        lines.append(f"- {table['name']}: {columns}")
    return "\n".join(lines)


def _allowed_from_schema(schema: dict[str, Any]) -> set[str]:
    return {str(table["name"]).lower() for table in schema.get("tables", [])}


def _validate_sql(
    config: dict[str, Any],
    database_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    schema = _schema(config, database_id)
    validation = _ensure_read_only_select(
        str(arguments.get("sql", "")),
        source_dialect=arguments.get("source_dialect", DEFAULT_SOURCE_DIALECT),
        target_dialect=config["dialect"],
        allowed_tables=_allowed_from_schema(schema),
        max_rows=_normalize_max_rows(arguments.get("max_rows")),
    )
    return {
        "success": True,
        "action": "validate",
        "database_id": database_id,
        "dialect": config["dialect"],
        "read_only": True,
        "executed": False,
        "schema_context": _schema_context(schema),
        "connection": _redact_connection(config),
        **validation,
    }


def _sql_generation_messages(
    *,
    request: str,
    schema_context: str,
    source_dialect: str,
    max_rows: int,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Generate exactly one read-only SQL SELECT statement. "
                f"Use {source_dialect} syntax. Use only listed tables and columns. "
                f"Include LIMIT no greater than {max_rows}. Return JSON with key sql."
            ),
        },
        {
            "role": "user",
            "content": f"{schema_context}\n\nRequest:\n{request}",
        },
    ]


def _strip_sql_response(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _call_sql_generation_llm(root: Path, messages: list[dict[str, str]]) -> str:
    store = ModelConfigStore(workspace_autonomy_home(root))
    provider = create_provider(store.load(), store)
    response = provider.complete_json(
        {"messages": messages},
        {
            "title": "SqlGeneration",
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    )
    sql = response.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise DatabaseRetrievalError("model SQL generation response is invalid")
    return sql


def _generate_sql(
    root: Path,
    config: dict[str, Any],
    database_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    request = str(arguments.get("request") or arguments.get("user_request") or "").strip()
    if not request:
        raise DatabaseRetrievalError("request must not be empty for action=generate")
    schema = _schema(config, database_id)
    source_dialect = _normalize_dialect(
        arguments.get("source_dialect", DEFAULT_SOURCE_DIALECT),
        default=DEFAULT_SOURCE_DIALECT,
    )
    messages = _sql_generation_messages(
        request=request,
        schema_context=_schema_context(schema),
        source_dialect=source_dialect,
        max_rows=_normalize_max_rows(arguments.get("max_rows")),
    )
    raw_sql = _strip_sql_response(_call_sql_generation_llm(root, messages))
    validation = _validate_sql(
        config,
        database_id,
        {**arguments, "sql": raw_sql, "source_dialect": source_dialect},
    )
    return {
        **validation,
        "action": "generate",
        "request": request,
        "raw_sql": raw_sql,
    }


def database_retrieve(root: Path, arguments: dict[str, Any]) -> Observation:
    try:
        action = str(arguments.get("action", "query")).strip().lower()
        if action == "connections":
            payload = {
                "success": True,
                "read_only": True,
                "connection_ids": sorted(_load_connections(root)),
            }
        else:
            database_id = str(arguments.get("database_id", "")).strip()
            if not database_id:
                raise DatabaseRetrievalError("database_id must not be empty")
            config = _connection(root, database_id)
            if action == "schema":
                payload = _schema(config, database_id)
            elif action == "healthcheck":
                payload = _query(
                    config,
                    database_id,
                    {"sql": "SELECT 1 AS healthcheck_ok", "max_rows": 1},
                )
            elif action == "validate":
                payload = _validate_sql(config, database_id, arguments)
            elif action == "generate":
                payload = _generate_sql(root, config, database_id, arguments)
            elif action == "explain":
                payload = _explain(config, database_id, arguments)
            elif action in {"query", "retrieve"}:
                generated = None
                if not str(arguments.get("sql", "")).strip() and str(
                    arguments.get("request") or arguments.get("user_request") or ""
                ).strip():
                    generated = _generate_sql(root, config, database_id, arguments)
                    arguments = {**arguments, "sql": generated["sql"]}
                payload = _query(config, database_id, arguments)
                payload["action"] = action
                if generated is not None:
                    payload.update(
                        {
                            "generated": True,
                            "request": generated["request"],
                            "raw_sql": generated["raw_sql"],
                            "generated_sql": generated["sql"],
                        }
                    )
            else:
                raise DatabaseRetrievalError(
                    "action must be one of connections, explain, generate, healthcheck, "
                    "schema, validate, query, retrieve"
                )
    except (
        ModelClientError,
        OSError,
        ProviderConfigurationError,
        TypeError,
        ValueError,
        sqlite3.Error,
        sqlglot.errors.SqlglotError,
    ) as exc:
        return Observation("", False, error=str(exc), evidence=("database_retrieve:error",))
    return Observation(
        "",
        True,
        output=json.dumps(payload, sort_keys=True),
        evidence=(
            f"database:{payload.get('database_id', 'connections')}",
            f"database_action:{action}",
        ),
    )


def validate_database_retrieve(arguments: dict[str, Any]) -> None:
    action = str(arguments.get("action", "query")).strip().lower()
    if action not in {
        "connections",
        "explain",
        "generate",
        "healthcheck",
        "schema",
        "validate",
        "query",
        "retrieve",
    }:
        raise ValueError(
            "action must be one of connections, explain, generate, healthcheck, schema, "
            "validate, query, retrieve"
        )
    if action != "connections" and not str(arguments.get("database_id", "")).strip():
        raise ValueError("database_id must not be empty")
    if action in {"explain", "validate", "query"}:
        _ensure_read_only_select(
            str(arguments.get("sql", "")),
            source_dialect=arguments.get("source_dialect", DEFAULT_SOURCE_DIALECT),
        )
    if action in {"generate", "retrieve"} and not (
        str(arguments.get("sql", "")).strip()
        or str(arguments.get("request") or arguments.get("user_request") or "").strip()
    ):
        raise ValueError("sql or request must be provided")
    if "max_rows" in arguments:
        _normalize_max_rows(arguments["max_rows"])


def register_database_tools(registry: ToolRegistry, root: Path) -> None:
    registry.register(
        "database.retrieve",
        lambda arguments: database_retrieve(root, arguments),
        validate_database_retrieve,
        description=(
            "Inspect configured databases, validate/transpile SQL with SQLGlot, generate "
            "read-only SQL from natural language with the workspace model, and run bounded "
            "SQLite SELECT queries. Connections live in <workspace>/.autonomy/database_connections.yaml."
        ),
        toolset="database",
        argument_contract={
            "action": "connections|explain|generate|healthcheck|schema|validate|query|retrieve, default query",
            "database_id": "configured database id, except for action=connections",
            "sql": "read-only SELECT or WITH query for explain/validate/query/retrieve (optional for generate/retrieve with request)",
            "request": "natural-language retrieval request for generate or retrieve (optional)",
            "source_dialect": "SQLGlot source dialect, default postgres (optional)",
            "max_rows": "integer max returned rows, default 100, max 1000 (optional)",
        },
        default_risk=RiskLevel.LOW,
    )
