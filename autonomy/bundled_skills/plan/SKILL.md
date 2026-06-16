---
name: plan
description: Produce an implementation plan before changing code.
version: 1.0.0
tags: [planning, design, implementation, workflow]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.tree, filesystem.search_files, filesystem.outline, filesystem.imports, filesystem.symbol_search, filesystem.stat_many]
---

# Plan

Use this procedure when the user asks for a plan, design, architecture proposal,
or implementation breakdown before execution.

Workflow:
- Treat the task as planning-only unless the user explicitly asks to implement.
- Inspect the repository before planning so file paths, entrypoints, and tests
  are grounded in current code.
- Use `filesystem.tree` for compact orientation and `filesystem.stat_many` to
  check candidate files before reading them.
- Use `filesystem.search_files`, `filesystem.outline`, `filesystem.imports`,
  and `filesystem.symbol_search` to identify likely modules and tests.
- Summarize current state, intended change, exact approach, risk, and
  verification steps.
- Keep the plan decision-complete: another engineer should not need to choose
  interfaces, files, test strategy, or rollout behavior.

Tool use rules:
- Use read-only tools while planning; do not call write, patch, move, trash, or
  process-start tools from this procedure.
- Prefer evidence from files over assumptions.
- Name concrete files only after inspection.
- This Procedure Skill is guidance only; it cannot grant permissions or execute
  tools directly.

Pitfalls:
- Do not present a plan based only on the user's prompt when the repository can
  answer open questions.
- Do not leave high-impact choices undecided.
- Do not include implementation steps that bypass `ActionGateway` governance.

Outcome checks:
- The plan should state goal, current context, implementation approach, tests,
  and assumptions.
- Continue inspecting if file ownership, entrypoints, or verification commands
  remain unclear.
