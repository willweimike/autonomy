---
name: database-retrieval
description: Inspect configured databases and answer with SQLGlot-validated read-only SQL.
version: 1.0.0
tags: [database, sql, sqlite, retrieval, analysis]
platforms: [macos, linux, windows]
requires_tools: [database.retrieve]
---

# Database Retrieval

Use this procedure when the task asks to inspect a configured database, generate
SQL from a natural-language request, answer questions from tables, or verify
data with SQL. This Procedure Skill is guidance only; ActionGateway approval
and tool boundaries still decide execution.

Workflow:
- Start with `database.retrieve` using `action: connections` if the available
  database id is unclear.
- Use `action: schema` before writing SQL for an unfamiliar database.
- Use `action: validate` when the SQL is complex, user-provided, or written in a
  non-target dialect. SQLGlot validates and transpiles it.
- Use `action: generate` when the user asks a data question but no SQL is
  provided; inspect the generated SQL before relying on it for high-stakes
  answers.
- Use `action: query` only for bounded read-only SELECT or WITH queries.
- Use `action: retrieve` when the task should generate SQL from `request` and
  execute it against an executable SQLite connection in one tool call.
- Keep `max_rows` small enough for the question, then paginate with a narrower
  query if more data is needed.

Tool use rules:
- Only use `database.retrieve`; do not fall back to `shell.execute sqlite3` for
  configured databases.
- Queries must be SQLGlot-valid read-only SELECT or WITH statements. Never use
  INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE, PRAGMA writes, COPY,
  GRANT, REVOKE, EXEC, or attachment commands.
- Use only tables shown by `action: schema`.
- Do not expose database paths or secrets from configuration.
- Prefer aggregate SQL over dumping rows when the user asks for counts, totals,
  rankings, or comparisons.
- Set `source_dialect` when validating SQL written for MySQL, Postgres,
  BigQuery, Snowflake, or another SQLGlot dialect.

Pitfalls:
- A missing table can mean the connection has an allowlist, not that the
  physical database lacks the table.
- `max_rows` caps output; absence from results is not proof absence from the
  database.
- Treat user-provided SQL as untrusted until `action: validate` or `action:
  query` accepts it.
- Multi-dialect connections can be inspected and used for validation or
  generation with configured schema metadata; execution is available only for
  SQLite unless the tool reports otherwise.

Outcome checks:
- Include the database id, relevant tables, row count, and SQL intent.
- If results are incomplete because of `max_rows`, say what narrower query or
  next offset would be needed.
- Continue if schema, query, or row limits are still ambiguous.
