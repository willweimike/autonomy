# Chrome Extension Design

## Goal

Add an Autonomy-specific Chrome extension that provides a graphical conversation interface while preserving Autonomy's existing execution boundary.

The extension is a UI only. It talks to a local Autonomy native messaging host, and the host routes requests into `ConversationLoop(interface="chrome")`. Tool execution still flows through `AgentLoop -> ActionGateway -> ToolRegistry`.

## Non-Goals

- A localhost HTTP or WebSocket server.
- A remote hosted Autonomy API.
- Direct tool execution from Chrome extension code.
- Reading or exposing `.autonomy/.env` secrets in the extension.
- Browser page DOM automation from the extension.
- Extension packaging for Chrome Web Store.

These can be added later when the local bridge is proven.

## User-Facing Shape

First version uses a Chrome side panel, not a popup.

The side panel provides:

- Workspace selector.
- Session status: workspace, model status, tool status.
- Chat input and response list.
- Run metadata: `run_id`, termination, steps executed.
- Inspect panel for a selected run journal summary.
- Approval modal for pending tool actions.

The approval modal shows:

- Tool name.
- Risk level.
- Redacted argument summary.
- Allow and Deny actions.

Deny is the default on timeout or disconnect.

## Architecture

```text
Chrome side panel
  -> MV3 service worker
  -> Native Messaging host
  -> autonomy chrome-host
  -> ConversationLoop(interface="chrome")
  -> AgentLoop -> ActionGateway -> ToolRegistry
```

Add:

- `autonomy/chrome_host.py`: native messaging stdio host and message framing.
- `autonomy/chrome_api.py`: small request handlers over `ConversationLoop`, `AutonomyStore`, and existing status helpers.
- `autonomy/cli.py`: `autonomy chrome-host` command.
- `chrome-extension/manifest.json`: Manifest V3 extension manifest.
- `chrome-extension/service_worker.js`: native port lifecycle and side panel message routing.
- `chrome-extension/sidepanel.html`
- `chrome-extension/sidepanel.js`
- `chrome-extension/sidepanel.css`

No new Python web framework is needed.

## Native Messaging Host

The host speaks Chrome native messaging framing:

- 4-byte little-endian message length.
- UTF-8 JSON payload.
- One JSON response per request.
- Hard message size cap.

The host command is:

```bash
autonomy chrome-host
```

The host keeps process-local session state while the native messaging port is open. Durable records remain in the existing workspace Autonomy database.

## Message Contract

Requests:

```json
{ "type": "status" }
{ "type": "session.start", "workspace": "/path/to/workspace", "max_steps": 12 }
{ "type": "chat.send", "session_id": "abc", "text": "Analyze this repo" }
{ "type": "run.inspect", "run_id": "abc" }
{ "type": "approval.respond", "approval_id": "abc", "decision": "allow" }
```

Responses:

```json
{ "ok": true, "type": "status.result", "workspace": "...", "model": "...", "tools": {...} }
{ "ok": true, "type": "session.started", "session_id": "abc" }
{ "ok": true, "type": "chat.result", "reply": "...", "run_id": "...", "termination": "completed", "steps_executed": 2 }
{ "ok": true, "type": "run.inspect.result", "run": {...} }
{ "ok": true, "type": "approval.result", "approval_id": "abc", "decision": "allow" }
{ "ok": false, "error": "model provider is not configured; run `autonomy model setup`" }
```

All response payloads must be JSON-serializable through existing `jsonable()` behavior where possible.

## Approval Flow

Autonomy already centralizes action execution at `ActionGateway`. Chrome approvals should plug into that boundary, not into individual tools.

Add an approval adapter that can:

- Receive an action approval request from `ActionGateway`.
- Emit an `approval.requested` host event to the extension.
- Wait for `approval.respond`.
- Return allow or deny to `ActionGateway`.
- Default to deny on timeout, disconnect, or malformed response.

The extension must not approve actions locally without a matching pending approval request from the host.

## Safety

Native host manifest:

- Restricts `allowed_origins` to the installed extension ID.
- Points to the installed `autonomy chrome-host` executable path.

Host validation:

- Reject non-object JSON.
- Reject missing or unknown `type`.
- Reject oversized messages.
- Validate `workspace` exists and is a directory.
- Redact secrets before sending errors or tool arguments to Chrome.

Runtime:

- Extension never reads workspace files directly.
- Extension never executes shell commands.
- Extension never receives provider API keys.
- Approval timeout defaults to deny.

## Data Flow

1. User opens Chrome side panel.
2. Service worker opens a native messaging port.
3. Side panel sends `status`.
4. User selects a workspace and sends `session.start`.
5. Host creates a `ConversationLoop` with `interface="chrome"`.
6. User sends `chat.send`.
7. Host calls `ConversationLoop.handle_user_input()`.
8. `AgentLoop` executes through `ActionGateway`.
9. If approval is required, host emits `approval.requested` and waits.
10. Extension shows approval modal and sends `approval.respond`.
11. Host returns final `chat.result`.
12. User can open `run.inspect` for details.

## Error Handling

- Missing model config: return existing provider setup error.
- Bad workspace path: return validation error.
- Native host framing error: return compact error if possible, then exit.
- Unknown session: return error and ask UI to start a session.
- Approval timeout: deny and continue the agent run.
- Agent failure: return `chat.result` with failed termination and reason, matching existing run behavior.

## Testing

Python tests:

- Native message framing reads and writes one JSON object.
- Oversized messages are rejected.
- Unknown request type returns `ok: false`.
- `session.start` creates a Chrome interface session.
- `chat.send` calls the conversation loop and returns reply/run metadata.
- `run.inspect` returns store journal data.
- Approval allow and deny are routed back to the approval adapter.
- Approval timeout defaults to deny.

Extension tests:

- Service worker opens native messaging port.
- Side panel sends `status`, `session.start`, and `chat.send`.
- Approval modal sends `approval.respond`.
- Side panel renders error responses without losing current transcript.

Targeted verification:

```bash
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q
python3.13 -m pytest tests/test_autonomy_native_cli.py -q
python3.13 -m pytest -q
python3.13 -m autonomy chrome-host --help
git diff --check
```

## Open Decisions

None for v1.

Implementation starts with the native host and Python tests, then adds the extension shell. Approval UI is included in v1 because it is required for a usable governed interface.
