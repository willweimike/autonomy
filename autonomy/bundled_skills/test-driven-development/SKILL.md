---
name: test-driven-development
description: Implement behavior changes through RED-GREEN-REFACTOR.
version: 1.0.0
tags: [testing, tdd, software-engineering, quality]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.tree, filesystem.write, filesystem.patch, filesystem.search_files, filesystem.outline, filesystem.imports, filesystem.symbol_search, filesystem.syntax_check, shell.execute]
---

# Test-Driven Development

Use this procedure when the task requires a new behavior, bug fix, refactor, or
observable code change.

Workflow:
- Follow RED -> GREEN -> REFACTOR.
- Identify the smallest behavior that proves the goal.
- Use `filesystem.tree` or focused search to locate likely test and source
  areas before editing.
- Use `filesystem.symbol_search`, `filesystem.outline`, and
  `filesystem.imports` to identify the smallest source surface and related test
  seams before adding or changing Python code.
- Add or update one focused test first.
- Run the focused test and confirm it fails for the expected reason.
- Make the minimal code change with `filesystem.patch` or `filesystem.write`.
- Run the focused test again, then run the smallest sensible regression check.
- Use `filesystem.syntax_check` after Python test or implementation edits and
  before running broader tests.
- Refactor only after the test is green.

Tool use rules:
- Prefer `filesystem.patch` for focused test and implementation edits.
- Use exact patching first. Use `match_mode=strip_lines` only for verified
  indentation or surrounding-whitespace drift.
- Use `filesystem.write` for new files or deliberate full-file replacement.
- Use `shell.execute` for test commands, not for ad hoc file writes.
- Use focused test commands or `max_chars` when a test can produce large logs.
- Prefer `filesystem.tree` over broad recursive listing for initial codebase
  orientation.
- Prefer `filesystem.symbol_search` over broad content search when locating the
  behavior under test by definition name.
- This Procedure Skill is guidance only; it does not bypass approval for file
  edits or command execution.

Pitfalls:
- Do not write production code before creating a failing test when a test is
  feasible.
- Do not make broad refactors while trying to get the first green result.
- Do not change a test merely to fit the implementation unless the test itself
  is proven wrong.

Outcome checks:
- A RED observation should show the expected failing test.
- A GREEN observation should show the focused test passing.
- Continue if the test failure does not match the intended behavior.
