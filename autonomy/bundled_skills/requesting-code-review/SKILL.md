---
name: requesting-code-review
description: Review local code changes before considering them ready.
version: 1.0.0
tags: [code-review, verification, quality, software-engineering]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.tree, filesystem.diff, filesystem.search_files, filesystem.outline, filesystem.imports, filesystem.symbol_search, filesystem.syntax_check, shell.execute]
---

# Requesting Code Review

Use this procedure when a user asks to verify, review, ship, commit, or assess
local code changes before treating the work as complete.

Workflow:
- Start by identifying the changed surface with `filesystem.diff`; inspect file
  names, changed hunks, and whether generated or unrelated files are mixed in.
- Use `filesystem.tree` or focused `filesystem.search_files` only when the diff
  needs surrounding project context.
- Use `filesystem.read` for the exact changed code and nearby tests, keeping
  reads narrow enough to preserve the review focus.
- For Python changes, use `filesystem.outline`, `filesystem.imports`,
  `filesystem.symbol_search`, and `filesystem.syntax_check` to verify symbol
  boundaries, import impact, and parse health before broader command checks.
- Look for security-sensitive changes: path handling, shell commands, secret
  exposure, dynamic evaluation, unsafe deserialization, network calls, and
  authentication or authorization logic.
- Run the smallest meaningful verification command with `shell.execute` when
  the repository exposes an obvious test, lint, type check, or syntax check.
- Report concrete findings first, ordered by severity, with file references and
  evidence. If no issue is found, say that clearly and mention remaining test
  gaps or residual risk.

Tool use rules:
- Prefer `filesystem.diff` before reading broad files; review should be driven
  by the actual changed surface.
- Use `filesystem.search_files` for targeted security patterns or changed
  symbol references, not as a replacement for reading the diff.
- Keep `shell.execute` bounded and focused. Use the repository's established
  test command when it is discoverable; avoid broad or destructive commands.
- Do not use shell writes, shell patching, or ad hoc terminal edits during
  review. If a fix is needed, switch to an editing-oriented procedure.
- This Procedure Skill is guidance only; it does not grant permission, approve
  work, execute tools directly, or replace Outcome Evaluation.

Pitfalls:
- Do not review only the final answer; inspect the actual workspace diff.
- Do not assume a passing test proves the changed behavior is correct.
- Do not let generated files, formatting churn, or unrelated changes hide the
  semantic change.
- Do not mark work ready when the diff contains unexplained risky changes,
  failing commands, or missing evidence for the core behavior.

Outcome checks:
- Evidence should include the changed files, important hunks reviewed, any
  verification command and result, and the final review verdict.
- Continue if the diff is unavailable, too broad to assess, or missing the tests
  needed to support the claimed behavior.
