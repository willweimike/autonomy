---
name: codebase-documentation
description: Generate workspace codebase documentation from inspected source.
version: 1.0.0
tags: [documentation, architecture, codebase, mermaid]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.write, filesystem.search_files, shell.execute]
---

# Codebase Documentation

Use this procedure when the user asks for repo documentation, architecture
notes, module maps, onboarding docs, or Mermaid-style diagrams.

Workflow:
- Scan manifests, README files, entrypoints, and top-level source directories.
- Pick a bounded set of important modules before writing.
- Read source files directly rather than inferring architecture from names.
- Write documentation only after evidence has been collected.
- Include architecture overview, entrypoints, module map, and validation notes.

Tool use rules:
- Use `filesystem.search_files` to find manifests, source files, and tests.
- Use `filesystem.read` for source evidence before `filesystem.write`.
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
