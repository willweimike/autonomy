---
name: website-inspection
description: Inspect a website by combining web search with browser snapshots when needed.
version: 1.0.0
tags: [web, browser, inspection]
platforms: [macos, linux, windows]
requires_tools: [web.search, browser.navigate, browser.snapshot, browser.screenshot, browser.get_images, browser.console, browser.dialog]
---

# Website Inspection

Use this procedure when the user asks to inspect, summarize, or verify the state
of a website.

Workflow:
- Use `web.search` first when the target URL is unknown or the user asks for
  general public information about a website.
- Use `browser.navigate` and `browser.snapshot` only if the page requires
  rendering, interaction, or visual page state.
- Keep browser snapshots compact first; request `full=true` or a larger
  `max_chars` only when the inspected content is truncated.
- Use `browser.get_images` when website media or image metadata is part of the
  inspection goal.
- Use `browser.screenshot` for visual layout evidence or when text extraction
  and snapshots do not capture the requested page state.
- Use `browser.console` to diagnose broken dynamic pages or missing client-side
  content.
- If a snapshot reports `pending_dialogs`, use `browser.dialog` before taking
  further snapshots or interactions.
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
- If browser tools are unavailable, continue with web search or report the
  limitation.
