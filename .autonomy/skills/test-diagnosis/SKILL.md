---
name: test-diagnosis
description: Diagnose software test failures by collecting reproducible evidence before identifying a root cause.
version: 1.0.0
tags: [testing, diagnosis, failures]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.list, search.text, shell.execute]
---

# Test Diagnosis

Use this procedure when the goal is to understand why tests fail.

## Workflow

1. Identify the repository's documented test command and required runtime.
2. Run the narrowest relevant test command that can reproduce the failure.
3. Preserve the exit code, failing test name, and exact error text.
4. Locate the failing implementation and its surrounding tests.
5. Separate code defects from environment, dependency, permission, and configuration failures.
6. State the root cause only when it explains the observed failure.

## Tool Rules

- Use `shell.execute` only for known diagnostic or test commands.
- Use `search.text` with exact error strings or symbol names.
- Read focused code and test files rather than entire directories.

## Pitfalls

- A nonzero exit code does not prove the application code is defective.
- Do not hide or paraphrase away the decisive error text.
- Do not claim a fix is valid without a confirming test result.

## Verification

Diagnosis is complete when the failure is reproducible and the proposed cause is tied to concrete command output and relevant code or configuration.
