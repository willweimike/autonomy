---
name: code-editing
description: Safely edit workspace text files through governed file tools.
version: 1.0.0
tags: [code, editing, filesystem]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.write, filesystem.patch, filesystem.search_files, shell.execute]
---

# Code Editing

Use this procedure when the task requires changing workspace source, tests, or
documentation files.

Workflow:
- Use `filesystem.search_files`, `filesystem.list`, or `filesystem.read` to
  locate and inspect the relevant text before editing.
- Prefer `filesystem.patch` for focused changes with a clear `old_string`.
- Use `filesystem.write` only when creating a new file or replacing the full
  content is simpler and safer than targeted patching.
- Run focused validation through `shell.execute` after editing when the project
  provides an obvious test command.

Tool use rules:
- Never use shell heredocs, `sed -i`, or ad hoc shell writes when
  `filesystem.write` or `filesystem.patch` can express the change.
- Keep edits inside the workspace.
- Do not edit binary files, generated caches, or `.git` contents.
- Small edits should use unique context in `old_string`; if the target text is
  ambiguous, read more context before patching.

Pitfalls:
- `filesystem.patch` with `replace_all=false` requires a unique `old_string`.
- `filesystem.write` overwrites the entire file; use it deliberately.
- A failed patch/write is evidence to re-read context, not a reason to bypass
  governance with shell commands.

Outcome checks:
- Successful edit observations should include the changed path and diff.
- Validation should be scoped to the edited behavior when possible.
