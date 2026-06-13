---
name: test-driven-development
description: Implement behavior changes through RED-GREEN-REFACTOR.
version: 1.0.0
tags: [testing, tdd, software-engineering, quality]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.write, filesystem.patch, filesystem.search_files, shell.execute]
---

# Test-Driven Development

Use this procedure when the task requires a new behavior, bug fix, refactor, or
observable code change.

Workflow:
- Follow RED -> GREEN -> REFACTOR.
- Identify the smallest behavior that proves the goal.
- Add or update one focused test first.
- Run the focused test and confirm it fails for the expected reason.
- Make the minimal code change with `filesystem.patch` or `filesystem.write`.
- Run the focused test again, then run the smallest sensible regression check.
- Refactor only after the test is green.

Tool use rules:
- Prefer `filesystem.patch` for focused test and implementation edits.
- Use `filesystem.write` for new files or deliberate full-file replacement.
- Use `shell.execute` for test commands, not for ad hoc file writes.
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
