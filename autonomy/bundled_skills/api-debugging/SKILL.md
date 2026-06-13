---
name: api-debugging
description: Diagnose REST and GraphQL integrations layer by layer.
version: 1.0.0
tags: [api, rest, graphql, debugging, integration]
platforms: [macos, linux, windows]
requires_tools: [web.fetch, web.extract, shell.execute]
---

# API Debugging

Use this procedure when REST, GraphQL, webhook, or HTTP integrations return an
unexpected status, body, timeout, or semantic result.

Workflow:
- Confirm connectivity and the exact URL or endpoint under investigation.
- Separate connection timeout, response timeout, TLS, authentication, request
  format, response parsing, and domain semantics.
- Use `web.fetch` or bounded `shell.execute` curl commands for concrete
  request/response evidence.
- For GraphQL, inspect response `errors` even when HTTP status is 200.
- Compare API behavior against relevant docs with `web.extract` when available.

Tool use rules:
- Do not expose or invent secrets in requests.
- Prefer read-only diagnostics unless the user explicitly asks for a write
  operation against an API.
- Keep evidence to status, headers that matter, and short response excerpts.
- This Procedure Skill is guidance only; approval and tool boundaries still
  decide execution.

Pitfalls:
- Do not treat HTTP 200 as proof the operation succeeded.
- Do not ignore redirects, content type, rate limits, or pagination.
- Do not use insecure TLS bypasses as implementation guidance.

Outcome checks:
- The observation should isolate which layer failed and include concrete
  request/response evidence.
- Continue if the failure layer is still unknown.
