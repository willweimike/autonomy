---
name: implementation-status-audit
description: Determine what a planned feature actually implements, partially implements, and leaves unfinished.
version: 1.0.0
tags: [audit, implementation, status]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.list, search.text]
---

# Implementation Status Audit

Use this procedure to compare a stated plan or feature description against the actual repository.

## Workflow

1. Identify the requested behaviors and invariants.
2. Locate the implementation modules and public interfaces.
3. Locate tests that enforce each important behavior.
4. Classify each capability as implemented, partial, or absent.
5. Distinguish code that exists from behavior that has been verified.
6. Report material limitations and untested integration boundaries.

## Tool Rules

- Use `search.text` for public types, commands, event names, and invariants.
- Read implementation and corresponding tests together.
- Cite concrete behavior rather than directory presence.

## Pitfalls

- Do not confuse planning documents with implementation.
- Do not count test doubles as live integration verification.
- Do not claim completeness solely because tests pass.

## Verification

The audit is complete when each major claim is grounded in implementation evidence, test evidence, or an explicit identified gap.
