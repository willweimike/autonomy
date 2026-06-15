---
name: browser-navigation
description: Navigate and inspect interactive web pages with governed browser tools.
version: 1.0.0
tags: [browser, navigation]
platforms: [macos, linux, windows]
requires_tools: [browser.navigate, browser.snapshot, browser.click, browser.type, browser.scroll, browser.back, browser.press, browser.screenshot, browser.get_images, browser.console, browser.dialog]
---

# Browser Navigation

Use this procedure when the task requires interacting with a page, form, dynamic
content, or navigation state.

Workflow:
- Start with `browser.navigate` for the target URL.
- Use `browser.snapshot` after navigation or interaction to inspect current
  state and collect actionable `elements`.
- Keep snapshots compact by default; use `full=true` or a larger `max_chars`
  only when the visible text is truncated or the task needs broader context.
- Use only selectors that appear in `browser.snapshot` `elements` for
  `browser.click` and `browser.type`.
- Use `browser.scroll`, `browser.back`, or `browser.press` only when they are
  needed to reveal or reach information.
- Use `browser.get_images` when the goal requires finding image assets, image
  alt text, or page media inventory.
- Use `browser.screenshot` when the task depends on visual layout, rendered
  state, or evidence that the text snapshot cannot capture.
- Use `browser.console` when the page appears broken, dynamic content is
  missing, or JavaScript errors may explain the current state.
- If `browser.snapshot` reports `pending_dialogs`, use `browser.dialog` to
  accept or dismiss the specific dialog before continuing page interaction.

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
- Do not request full snapshots repeatedly when compact snapshots already show
  the relevant state.
- Do not use `browser.console` expression evaluation for broad automation; keep
  it to diagnostics or small DOM state checks.
- Do not take repeated screenshots when a text snapshot or image inventory
  already provides enough evidence.
- Do not ignore pending dialogs; they can block navigation and follow-up
  interactions until handled.

Outcome checks:
- The snapshot should show the URL, title, relevant visible page text, and
  actionable elements when interaction is needed.
- Screenshot observations should include the PNG path and current URL.
- Console output can explain silent page failures, but visible page state still
  needs a snapshot when the task depends on what the user would see.
- Continue if page state is ambiguous.
