---
name: process-management
description: Manage long-running terminal processes through governed process tools.
version: 1.0.0
tags: [terminal, process, server, testing]
platforms: [macos, linux, windows]
requires_tools: [shell.execute, process.start, process.poll, process.log, process.wait, process.stop]
---

# Process Management

Use this procedure when the task requires a dev server, watcher, long-running
test command, or any terminal command that should not block the whole run.

Workflow:
- Use `shell.execute` for short, bounded commands.
- Use `process.start` for long-running commands such as dev servers, watch
  tasks, or commands that need later inspection.
- After `process.start`, use `process.poll` or `process.log` to collect current
  output before deciding the next action.
- Use `process.wait` only with a small explicit timeout when the process is
  expected to finish soon.
- Use `process.stop` when a background process is no longer needed.

Tool use rules:
- Keep `workdir` inside the workspace.
- Do not start duplicate servers or watchers without checking existing process
  state.
- Treat `process_id` values as run-local handles from prior observations.
- Do not use shell writes or ad hoc terminal edits when file tools can express
  the change.

Pitfalls:
- A `process.wait` timeout means the process is still running; inspect logs
  instead of assuming failure.
- Starting a process can have side effects and may require approval.
- Background process state is local to the current run.

Outcome checks:
- Evidence should include the `process_id`, process status, recent output, and
  exit code when available.
- Continue if logs show the process is still starting or more evidence is
  needed.
