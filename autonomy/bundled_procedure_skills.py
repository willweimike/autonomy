from __future__ import annotations


BUNDLED_PROCEDURE_SKILLS: dict[str, str] = {
    "web-research": """---
name: web-research
description: Research a web page using governed fetch and extract tools.
version: 1.0.0
tags: [web, research]
platforms: [macos, linux, windows]
requires_tools: [web.fetch, web.extract]
---

# Web Research

Use this procedure when the task asks for information from a specific HTTP or
HTTPS page.

Workflow:
- Prefer `web.extract` when the user needs readable page content.
- Use `web.fetch` when status, content type, redirects, or raw body evidence
  matters.
- Keep requests focused on the URLs needed for the goal.
- Compare extracted content against the user goal before proposing another
  action.

Tool use rules:
- Only use `http` or `https` URLs.
- Use `max_chars` when a page may be large.
- Treat redirects and non-2xx responses as evidence, not as automatic success.

Pitfalls:
- Do not infer facts that are not present in the fetched or extracted content.
- Do not use browser tools unless interaction with the page is required.

Outcome checks:
- The observation should include URL, status, and relevant page text or body.
- Continue if the content is insufficient for the goal.
""",
    "browser-navigation": """---
name: browser-navigation
description: Navigate and inspect interactive web pages with governed browser tools.
version: 1.0.0
tags: [browser, navigation]
platforms: [macos, linux, windows]
requires_tools: [browser.navigate, browser.snapshot, browser.click, browser.type, browser.scroll, browser.back, browser.press, browser.get_images, browser.console]
---

# Browser Navigation

Use this procedure when the task requires interacting with a page, form, dynamic
content, or navigation state.

Workflow:
- Start with `browser.navigate` for the target URL.
- Use `browser.snapshot` after navigation or interaction to inspect current
  state and collect actionable `elements`.
- Use only selectors that appear in `browser.snapshot` `elements` for
  `browser.click` and `browser.type`.
- Use `browser.scroll`, `browser.back`, or `browser.press` only when they are
  needed to reveal or reach information.
- Use `browser.get_images` when the goal requires finding image assets, image
  alt text, or page media inventory.
- Use `browser.console` when the page appears broken, dynamic content is
  missing, or JavaScript errors may explain the current state.

Tool use rules:
- Browser actions are medium risk and may require approval.
- Prefer read-only `web.extract` when interaction is not necessary.
- Keep selectors specific and avoid broad destructive clicks.
- Do not invent selectors. If no suitable element appears in the snapshot,
  take another snapshot, scroll, or report the limitation.

Pitfalls:
- Do not assume a click succeeded without a follow-up snapshot.
- Do not repeat the same interaction unless the latest snapshot shows a reason.
- Do not use selectors from memory after navigation changes the page state.
- Do not use `browser.console` expression evaluation for broad automation; keep
  it to diagnostics or small DOM state checks.

Outcome checks:
- The snapshot should show the URL, title, relevant visible page text, and
  actionable elements when interaction is needed.
- Console output can explain silent page failures, but visible page state still
  needs a snapshot when the task depends on what the user would see.
- Continue if page state is ambiguous.
""",
    "process-management": """---
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
""",
    "website-inspection": """---
name: website-inspection
description: Inspect a website by combining web extraction with browser snapshots when needed.
version: 1.0.0
tags: [web, browser, inspection]
platforms: [macos, linux, windows]
requires_tools: [web.extract, browser.navigate, browser.snapshot, browser.get_images, browser.console]
---

# Website Inspection

Use this procedure when the user asks to inspect, summarize, or verify the state
of a website.

Workflow:
- Use `web.extract` first for static page content.
- Use `browser.navigate` and `browser.snapshot` only if the page requires
  rendering, interaction, or visual page state.
- Use `browser.get_images` when website media or image metadata is part of the
  inspection goal.
- Use `browser.console` to diagnose broken dynamic pages or missing client-side
  content.
- Compare the extracted text and browser snapshot before drawing conclusions.
- If interaction becomes necessary, rely on snapshot `elements` selectors.

Tool use rules:
- Keep the inspection limited to the requested site or page.
- Do not use click/type actions unless the user goal requires interaction.
- Record page URL and title as evidence.
- Do not invent selectors; use only selectors observed in browser snapshots.

Pitfalls:
- Dynamic pages may expose different content through fetch and browser snapshot.
- Browser availability depends on Playwright and Chromium runtime.

Outcome checks:
- The final evidence should include the inspected URL and relevant content.
- If browser tools are unavailable, continue with web extraction or report the
  limitation.
""",
    "code-editing": """---
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
""",
}


def bundled_skill_names() -> tuple[str, ...]:
    return tuple(sorted(BUNDLED_PROCEDURE_SKILLS))
