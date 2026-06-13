---
name: systematic-debugging
description: Diagnose bugs and failures by finding root cause before fixing.
version: 1.0.0
tags: [debugging, testing, root-cause, software-engineering]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.search_files, shell.execute]
---

# Systematic Debugging

Use this procedure when the task involves a bug, failing test, build failure,
unexpected behavior, or regression.

Workflow:
- Reproduce the failure with the smallest useful command before proposing a fix.
- Read the complete error, stack trace, failing assertion, or command output.
- Use `filesystem.search_files` and `filesystem.read` to trace the failing
  symbol, data path, or configuration path.
- Look for a nearby working example before editing.
- Form a concrete root-cause hypothesis before selecting a code change.

Tool use rules:
- Use `shell.execute` only for bounded reproduction and inspection commands.
- Use read/search tools before any edit-oriented procedure.
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
