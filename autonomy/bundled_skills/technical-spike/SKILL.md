---
name: technical-spike
description: Run disposable feasibility experiments before committing to a build.
version: 1.0.0
tags: [spike, prototype, feasibility, experiment]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.tree, filesystem.write, filesystem.patch, filesystem.search_files, filesystem.outline, filesystem.imports, filesystem.symbol_search, filesystem.syntax_check, shell.execute, process.start, process.poll, process.log, process.wait, process.stop]
---

# Technical Spike

Use this procedure when the task asks whether an approach is feasible, compares
implementation options, or needs a quick prototype before production work.

Workflow:
- Convert the idea into 2 to 5 observable feasibility questions.
- Prioritize the riskiest question first.
- Use `filesystem.tree` to understand the smallest relevant project area before
  creating spike artifacts.
- For Python spikes inside an existing codebase, use `filesystem.outline`,
  `filesystem.imports`, and `filesystem.symbol_search` to choose the narrowest
  integration point instead of scanning full files.
- Build only the smallest disposable artifact needed to answer the question.
- Use `process.start` for demos, servers, or long-running experiments that need
  later inspection.
- Record a verdict: validated, partial, or invalidated.

Tool use rules:
- Keep spike artifacts inside the workspace and clearly separated from
  production paths unless the user asks otherwise.
- Use file tools for spike files and terminal/process tools for running them.
- Use compact tree output before broad listing when choosing where to place a
  disposable artifact.
- Use `filesystem.syntax_check` on Python spike files before running longer
  commands or background processes.
- Bound noisy command output with `max_chars` and use `process.log` windows
  instead of repeatedly dumping full logs.
- Stop background processes when the spike evidence has been collected.
- This Procedure Skill is guidance only; it does not make spike output trusted
  production code.

Pitfalls:
- Do not turn a spike into production implementation without a follow-up plan.
- Do not declare success from one happy-path output if edge cases matter.
- Do not start duplicate servers or watchers without checking process state.

Outcome checks:
- The observation should include runnable evidence or a concrete failure.
- The final spike result should state validated, partial, or invalidated and
  explain the production recommendation.
