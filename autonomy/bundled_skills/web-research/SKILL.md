---
name: web-research
description: Research a web page using governed fetch and extract tools.
version: 1.0.0
tags: [web, research]
platforms: [macos, linux, windows]
requires_tools: [web.fetch, web.extract, web.links]
---

# Web Research

Use this procedure when the task asks for information from a specific HTTP or
HTTPS page.

Workflow:
- Prefer `web.extract` when the user needs readable page content.
- Use `web.fetch` when status, content type, redirects, or raw body evidence
  matters.
- Use `web.links` when the next useful URL must be chosen from a known page.
- Keep requests focused on the URLs needed for the goal.
- Compare extracted content against the user goal before proposing another
  action.

Tool use rules:
- Only use `http` or `https` URLs.
- Use `max_chars` when a page may be large.
- Use `max_links` when a page may contain many navigation links.
- Treat redirects and non-2xx responses as evidence, not as automatic success.

Pitfalls:
- Do not infer facts that are not present in the fetched or extracted content.
- Do not use browser tools unless interaction with the page is required.

Outcome checks:
- The observation should include URL, status, and relevant page text or body.
- Continue if the content is insufficient for the goal.
