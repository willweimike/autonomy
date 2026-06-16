---
name: web-research
description: Research public web information using governed search tools.
version: 1.0.0
tags: [web, research]
platforms: [macos, linux, windows]
requires_tools: [web.search]
---

# Web Research

Use this procedure when the task asks for public information from the web.

Workflow:
- Use `web.search` for entity lookup, background questions, public facts, and
  general web discovery.
- Keep the query focused on the user's exact subject.
- Use a small `limit` first unless the user asks for broad research.
- Compare result titles and snippets against the user goal before proposing
  another action.

Tool use rules:
- Provide a non-empty `query`.
- Use `limit` when the answer only needs a few sources.
- Treat snippets as search-result evidence, not as complete page evidence.

Pitfalls:
- Do not infer facts that are not present in search titles or snippets.
- Do not use browser tools unless interaction with a page is required.

Outcome checks:
- The observation should include ranked result titles, URLs, and snippets.
- Continue if the content is insufficient for the goal.
