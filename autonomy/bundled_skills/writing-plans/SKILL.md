---
name: writing-plans
description: Write decision-complete implementation plans for software changes.
version: 1.0.0
tags: [planning, implementation, tdd, documentation]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.read_many, filesystem.tree, filesystem.search_files, filesystem.outline, filesystem.imports, filesystem.symbol_search, filesystem.syntax_check, filesystem.stat_many]
---

# Writing Plans

Use this procedure when a software task needs a plan detailed enough for a
different engineer or agent to implement safely.

Workflow:
- Start by identifying the concrete goal, success criteria, constraints, and
  out-of-scope work.
- Inspect current code and tests before proposing files or APIs.
- Use `filesystem.read_many` for small batches of manifests, README files,
  entrypoints, and relevant tests.
- Use `filesystem.outline`, `filesystem.imports`, and
  `filesystem.symbol_search` to map implementation surfaces before reading
  large files.
- Break implementation into ordered, small tasks with exact files, behavior,
  test cases, and verification commands.
- Include failure modes and rollback or recovery notes when the change touches
  persistence, tools, configuration, or user-facing CLI behavior.

Tool use rules:
- Keep planning read-only unless the user explicitly asks to save the plan.
- Prefer `filesystem.stat_many` and `filesystem.tree` before broad recursive
  reads.
- If the plan includes edits, specify `filesystem.patch` or `filesystem.write`
  as the intended governed tools; do not prescribe shell heredocs or in-place
  shell edits.
- This Procedure Skill is guidance only; it does not execute, authorize, or
  validate actions.

Pitfalls:
- Do not write vague tasks like "update the runtime"; name the target behavior
  and file surfaces.
- Do not omit tests for negative paths, governance boundaries, or persistence.
- Do not over-design future extension points that are not needed for the stated
  goal.

Outcome checks:
- The plan should be decision-complete: approach, interfaces, data flow, edge
  cases, tests, and assumptions are all explicit.
- Continue gathering evidence if the implementer would still need to guess
  where to make the change.
