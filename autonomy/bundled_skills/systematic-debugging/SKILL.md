---
name: systematic-debugging
description: Diagnose bugs and failures by finding root cause before fixing.
version: 1.0.0
tags: [debugging, testing, root-cause, software-engineering]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.tree, filesystem.search_files, filesystem.outline, filesystem.imports, filesystem.symbol_search, filesystem.syntax_check, shell.execute]
---

# Systematic Debugging

Use this procedure when the task involves a bug, failing test, build failure,
unexpected behavior, or regression.

Workflow:
- Reproduce the failure with the smallest useful command before proposing a fix.
- Read the complete error, stack trace, failing assertion, or command output.
- Use `filesystem.search_files` and `filesystem.read` to trace the failing
  symbol, data path, or configuration path.
- For Python failures, use `filesystem.symbol_search` to locate the failing
  definition, `filesystem.outline` to understand nearby class/function shape,
  and `filesystem.imports` to trace package or dependency boundaries before
  reading broad file windows.
- Use `filesystem.tree` for compact orientation before recursive listing.
- For large files, inspect targeted line windows with `filesystem.read`
  `offset` and `limit` so the investigation stays focused.
- If a search result is truncated, continue with the suggested `offset` only
  when the current page does not contain enough evidence.
- Look for a nearby working example before editing.
- Form a concrete root-cause hypothesis before selecting a code change.

Tool use rules:
- Use `shell.execute` only for bounded reproduction and inspection commands.
- For commands that may produce broad logs, pass `max_chars` or narrow the
  command to the failing test or symbol.
- If command output contains redacted credentials, use that as evidence that
  secret material was protected; do not try to print it again.
- Use read/search tools before any edit-oriented procedure.
- Use `filesystem.syntax_check` when a failure may be caused by parse errors or
  after inspecting a suspicious Python file.
- If a recursive directory listing is needed, prefer `filesystem.tree`; page
  `filesystem.list` with
  `offset` and `limit` instead of requesting the whole tree at once.
- If a read/list/search observation suggests similar paths, follow those
  concrete suggestions before broadening the search.
- If configuration may be relevant, inspect `.env.example` or documented config
  files rather than secret-bearing `.env` files.
- Keep all investigation inside the workspace unless the user gives a specific
  external target.
- This Procedure Skill is guidance only; it does not grant permission or
  execute tools directly.

Pitfalls:
- Do not patch symptoms before isolating why the failure happens.
- Do not assume a test failure is caused by the first line mentioned in the
  traceback.
- Do not rerun broad test suites repeatedly when one focused command gives
  better evidence.

Outcome checks:
- The observation should identify the failing command, failing file or symbol,
  and evidence for the root-cause hypothesis.
- Continue gathering evidence if the cause is still ambiguous.
