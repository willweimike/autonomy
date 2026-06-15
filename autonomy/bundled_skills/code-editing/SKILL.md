---
name: code-editing
description: Safely edit workspace text files through governed file tools.
version: 1.0.0
tags: [code, editing, filesystem]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read, filesystem.tree, filesystem.write, filesystem.patch, filesystem.search_files, filesystem.outline, filesystem.imports, filesystem.symbol_search, filesystem.syntax_check, shell.execute]
---

# Code Editing

Use this procedure when the task requires changing workspace source, tests, or
documentation files.

Workflow:
- Use `filesystem.tree`, `filesystem.search_files`, `filesystem.list`, or `filesystem.read` to
  locate and inspect the relevant text before editing.
- Use `filesystem.stat` when you need cheap metadata about one path before
  deciding whether to read, list, move, or modify it; use
  `filesystem.stat_many` when checking several candidate paths together.
- For Python source, use `filesystem.outline`, `filesystem.imports`, or
  `filesystem.symbol_search` to find the target class, function, method, and
  dependency edges before reading large files.
- For large files, read focused windows with `filesystem.read` `offset` and
  `limit` instead of loading the whole file.
- Use `filesystem.read_many` for small batches of manifests, README files,
  entrypoints, or nearby tests that should be inspected together.
- When directory listings or search results are broad, use `limit` and
  `offset` to inspect the next page rather than repeating the same query.
- Prefer `filesystem.patch` for focused changes with a clear `old_string`.
- Keep `filesystem.patch` in exact mode by default; use
  `match_mode=strip_lines` only after reading the current file and confirming
  the intended lines differ only by indentation or surrounding whitespace.
- When a recent `filesystem.stat`, `filesystem.stat_many`, or `filesystem.read`
  observation provides a `revision`, pass it as `expected_revision` to
  `filesystem.patch` or `filesystem.write` so the edit fails if the file changed
  after inspection.
- Use `filesystem.write` only when creating a new file or replacing the full
  content is simpler and safer than targeted patching.
- Use `filesystem.mkdir` for new directories and `filesystem.move` for
  renaming or relocating workspace files; avoid shell `mkdir` and `mv`.
- Run focused validation through `shell.execute` after editing when the project
  provides an obvious test command.
- Use `filesystem.diff` after edits to inspect bounded git status and diff
  evidence without invoking shell `git diff`.
- After editing Python files, use `filesystem.syntax_check` before broader test
  commands so syntax regressions are caught with cheap deterministic feedback.

Tool use rules:
- Never use shell heredocs, `sed -i`, or ad hoc shell writes when
  `filesystem.write` or `filesystem.patch` can express the change.
- When removing an obsolete workspace file or directory, use
  `filesystem.trash` if it is available; never use shell `rm`, `rmdir`, or
  `rm -rf`.
- Prefer `filesystem.tree` for initial project orientation over broad recursive
  `filesystem.list` output.
- Prefer `filesystem.symbol_search` for locating definitions over broad content
  search when the target is a class, function, or method name.
- Prefer `filesystem.imports` when the edit depends on import structure,
  package boundaries, or integration points.
- Keep edits inside the workspace.
- Do not edit binary files, generated caches, or `.git` contents.
- Do not read or edit secret-bearing environment files such as `.env`,
  `.env.local`, or `.envrc`; inspect `.env.example` for configuration shape.
- Small edits should use unique context in `old_string`; if the target text is
  ambiguous, read more context before patching.
- If a file or directory path is missing and the observation includes similar
  paths, inspect the suggested path instead of guessing a new path.

Pitfalls:
- `filesystem.patch` with `replace_all=false` requires a unique `old_string`.
- `match_mode=strip_lines` can still be ambiguous; if it fails, read more
  context and provide a longer line sequence.
- `filesystem.write` overwrites the entire file; use it deliberately.
- A failed patch/write is evidence to re-read context, not a reason to bypass
  governance with shell commands.

Outcome checks:
- Successful edit observations should include the changed path and diff.
- Validation should be scoped to the edited behavior when possible.
