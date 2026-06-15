---
name: codebase-documentation
description: Generate workspace codebase documentation from inspected source.
version: 1.0.0
tags: [documentation, architecture, codebase, mermaid]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.tree, filesystem.write, filesystem.search_files, filesystem.outline, filesystem.imports, filesystem.symbol_search, filesystem.syntax_check, shell.execute]
---

# Codebase Documentation

Use this procedure when the user asks for repo documentation, architecture
notes, module maps, onboarding docs, or Mermaid-style diagrams.

Workflow:
- Scan manifests, README files, entrypoints, and top-level source directories.
- Use a compact workspace tree to identify likely source, test, and docs areas.
- Use `filesystem.outline` and `filesystem.imports` to map Python module
  structure, entrypoints, and dependency boundaries before reading large source
  files.
- Use `filesystem.symbol_search` to find named public APIs, command handlers,
  or core loop components that should appear in the documentation.
- Pick a bounded set of important modules before writing.
- Read source files directly rather than inferring architecture from names.
- Write documentation only after evidence has been collected.
- Include architecture overview, entrypoints, module map, and validation notes.

Tool use rules:
- Use `filesystem.search_files` to find manifests, source files, and tests.
- Use `filesystem.tree` before broad recursive listing when orienting in a repo.
- Use `filesystem.read` for source evidence before `filesystem.write`.
- Use `filesystem.syntax_check` as lightweight evidence when documenting
  whether Python source currently parses, but do not treat parse success as a
  substitute for reading behavior.
- Page broad `filesystem.list` output with `offset` and `limit` before
  deciding which directories matter.
- For large source files, read focused windows with `offset` and `limit` and
  summarize only evidence that was actually inspected.
- Page broad `filesystem.search_files` results with `offset` and `limit` so
  documentation evidence stays bounded and reproducible.
- Keep generated documentation inside the workspace path requested by the user.
- This Procedure Skill is guidance only; write actions still require approval.

Pitfalls:
- Do not document generated caches, vendored dependencies, or `.git` contents.
- Do not create overly broad docs without first identifying the key modules.
- Do not invent workflows that are not visible in code or docs.

Outcome checks:
- The observation should include written documentation paths or the inspected
  evidence needed to continue.
- Continue if entrypoints or module boundaries are still unclear.
