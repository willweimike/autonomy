---
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
