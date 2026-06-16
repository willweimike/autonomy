---
name: repository-orientation
description: Establish an evidence-backed understanding of an unfamiliar repository before deeper work.
version: 1.0.0
tags: [repository, orientation, architecture]
platforms: [macos, linux, windows]
requires_tools: [filesystem.list, filesystem.read, search.text]
---

# Repository Orientation

Use this procedure when the repository structure, runtime, or test commands are not yet known.

## Workflow

1. List the repository root without recursively reading every file.
2. Read the highest-signal manifests and documentation, such as `README.md`, `pyproject.toml`, or `package.json`.
3. Locate tests, runtime entrypoints, and configuration files.
4. Search for the symbols or concepts directly related to the goal.
5. Summarize the architecture using concrete file evidence before proposing deeper actions.

## Tool Rules

- Prefer `filesystem.list` before broad searches.
- Use `filesystem.read` only for relevant files.
- Use `search.text` to locate concrete symbols or behavior.
- Do not infer runtime commands when a manifest or README can establish them.

## Pitfalls

- Avoid recursive full-repository reads.
- Do not treat documentation claims as implementation proof.
- Do not modify files during orientation.

## Verification

Orientation is sufficient when the relevant entrypoint, implementation area, test area, and runtime command are supported by observed evidence.
