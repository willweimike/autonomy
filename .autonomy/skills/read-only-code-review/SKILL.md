---
name: read-only-code-review
description: Review code for behavioral defects, regressions, governance violations, and missing tests without modifying files.
version: 1.0.0
tags: [review, quality, risk]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.list, search.text]
---

# Read-Only Code Review

Use this procedure when evaluating implementation quality without making changes.

## Workflow

1. Establish the intended behavior and ownership boundaries.
2. Read the changed or relevant implementation paths.
3. Trace important data and control flow across boundaries.
4. Compare implementation behavior with tests.
5. Identify concrete bugs, regressions, unsafe behavior, or missing coverage.
6. Rank findings by severity and explain their observable impact.

## Tool Rules

- Search for callers and tests before declaring code unused or incorrect.
- Read the smallest relevant slices that establish behavior.
- Treat generated output and documentation as supporting, not decisive, evidence.

## Pitfalls

- Avoid style-only findings unless they create operational risk.
- Do not report speculative issues without a plausible triggering path.
- Do not modify repository files during review.

## Verification

A finding is valid only when it identifies a triggering condition, affected behavior, and concrete implementation evidence.
