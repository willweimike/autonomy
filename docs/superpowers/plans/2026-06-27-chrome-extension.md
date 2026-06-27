# Chrome Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Chrome side panel UI that talks to local Autonomy through a Chrome Native Messaging host.

**Architecture:** Add a small stdio host behind `autonomy chrome-host`, route messages into `ConversationLoop(interface="chrome")`, and reuse `ApprovalPolicy(prompt=...)` for Chrome approvals. The extension remains UI-only; all execution still flows through `AgentLoop -> ActionGateway -> ToolRegistry`.

**Tech Stack:** Python 3.13 stdlib, existing Autonomy `ConversationLoop`, existing `ApprovalPolicy`, Chrome Manifest V3, Native Messaging, plain HTML/CSS/JS static files, pytest.

## Global Constraints

- First UI is Chrome side panel, not popup.
- Use Chrome Native Messaging; do not add localhost HTTP or WebSocket server.
- Extension code never executes tools directly.
- Extension code never reads workspace files directly.
- Extension never receives provider API keys or `.autonomy/.env` content.
- Tool execution stays behind `AgentLoop -> ActionGateway -> ToolRegistry`.
- Approval timeout and disconnect default to deny.
- Native host messages use 4-byte little-endian length plus UTF-8 JSON.
- Reject non-object JSON, unknown `type`, missing `type`, and oversized messages.
- Validate `workspace` exists and is a directory.
- Do not add Python or JavaScript package dependencies.
- Do not use `rm`, `rm -rf`, or `rmdir`; use `trash` for deletion.

---

## File Structure

- Create `autonomy/chrome_host.py`: Native Messaging framing, host loop, writer lock, request dispatcher, pending approval coordination.
- Create `autonomy/chrome_api.py`: request handlers for `status`, `session.start`, `chat.send`, `run.inspect`, `approval.respond`; bridge sessions to `ConversationLoop`.
- Modify `autonomy/cli.py`: add `chrome-host` command and workspace routing.
- Create `tests/test_autonomy_native_chrome_host.py`: Python tests for framing, request dispatch, sessions, chat, inspect, approval allow/deny/timeout.
- Create `chrome-extension/manifest.json`: MV3 side panel extension manifest.
- Create `chrome-extension/service_worker.js`: native port lifecycle and panel message routing.
- Create `chrome-extension/sidepanel.html`: chat UI markup.
- Create `chrome-extension/sidepanel.js`: panel state, message send/render, approval modal.
- Create `chrome-extension/sidepanel.css`: compact UI styling.
- Create `chrome-extension/native-host.example.json`: dev native host manifest template.
- Create `tests/test_chrome_extension_static.py`: static validation for manifest, HTML, and JS route names.
- Modify `README.md`: document extension dev install and native host manifest setup.

---

### Task 1: Native Messaging Framing and CLI Command

**Files:**
- Create: `autonomy/chrome_host.py`
- Modify: `autonomy/cli.py`
- Test: `tests/test_autonomy_native_chrome_host.py`

**Interfaces:**
- Produces: `MAX_NATIVE_MESSAGE_BYTES: int`
- Produces: `ChromeHostError(ValueError)`
- Produces: `read_native_message(stream: BinaryIO, *, max_bytes: int = MAX_NATIVE_MESSAGE_BYTES) -> dict[str, Any] | None`
- Produces: `write_native_message(stream: BinaryIO, payload: Mapping[str, Any]) -> None`
- Produces: `run_chrome_host(*, input_stream: BinaryIO | None = None, output_stream: BinaryIO | None = None, api: ChromeBridge | None = None) -> int`
- Produces CLI command: `python3.13 -m autonomy chrome-host`

- [ ] **Step 1: Write failing framing and CLI tests**

Create `tests/test_autonomy_native_chrome_host.py`:

```python
import io
import json
import struct
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from autonomy.cli import build_parser, main


def framed(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack("<I", len(body)) + body


class AutonomyNativeChromeHostTest(unittest.TestCase):
    def test_native_message_round_trips_json_object(self):
        from autonomy.chrome_host import read_native_message, write_native_message

        incoming = io.BytesIO(framed({"type": "status"}))
        self.assertEqual(read_native_message(incoming), {"type": "status"})

        outgoing = io.BytesIO()
        write_native_message(outgoing, {"ok": True, "type": "status.result"})

        size = struct.unpack("<I", outgoing.getvalue()[:4])[0]
        payload = json.loads(outgoing.getvalue()[4 : 4 + size].decode("utf-8"))
        self.assertEqual(payload, {"ok": True, "type": "status.result"})

    def test_native_message_rejects_non_object_and_oversized_payload(self):
        from autonomy.chrome_host import ChromeHostError, read_native_message

        with self.assertRaisesRegex(ChromeHostError, "expected JSON object"):
            read_native_message(io.BytesIO(framed(["bad"])))

        with self.assertRaisesRegex(ChromeHostError, "exceeds"):
            read_native_message(io.BytesIO(framed({"type": "status"})), max_bytes=2)

    def test_chrome_host_parser_and_main_delegate_to_host(self):
        args = build_parser().parse_args(["chrome-host"])
        self.assertEqual(args.command, "chrome-host")

        with (
            patch("autonomy.chrome_host.run_chrome_host", return_value=0) as run_host,
            redirect_stdout(io.StringIO()),
        ):
            result = main(["chrome-host"])

        self.assertEqual(result, 0)
        run_host.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autonomy.chrome_host'` or parser rejecting `chrome-host`.

- [ ] **Step 3: Add minimal native host framing**

Create `autonomy/chrome_host.py`:

```python
from __future__ import annotations

import json
import struct
import sys
from collections.abc import BinaryIO, Mapping
from typing import Any, Protocol


MAX_NATIVE_MESSAGE_BYTES = 1_000_000


class ChromeHostError(ValueError):
    pass


class ChromeBridge(Protocol):
    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        ...


class EchoStatusBridge:
    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("type") != "status":
            return {"ok": False, "error": "unknown request type"}
        return {"ok": True, "type": "status.result"}


def read_native_message(
    stream: BinaryIO,
    *,
    max_bytes: int = MAX_NATIVE_MESSAGE_BYTES,
) -> dict[str, Any] | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ChromeHostError("invalid native message header")
    size = struct.unpack("<I", header)[0]
    if size > max_bytes:
        raise ChromeHostError(f"native message exceeds {max_bytes} bytes")
    body = stream.read(size)
    if len(body) != size:
        raise ChromeHostError("truncated native message")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ChromeHostError("expected JSON object")
    return payload


def write_native_message(stream: BinaryIO, payload: Mapping[str, Any]) -> None:
    body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("<I", len(body)))
    stream.write(body)
    stream.flush()


def run_chrome_host(
    *,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
    api: ChromeBridge | None = None,
) -> int:
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    api = api or EchoStatusBridge()
    while True:
        try:
            message = read_native_message(input_stream)
            if message is None:
                return 0
            response = api.handle(message)
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        write_native_message(output_stream, response)
```

- [ ] **Step 4: Wire CLI command**

Modify `autonomy/cli.py`:

```python
# in build_parser()
subparsers.add_parser("chrome-host")
```

```python
# in main(), before workspace-bound commands
if args.command == "chrome-host":
    from .chrome_host import run_chrome_host

    return run_chrome_host()
```

Update `_workspace_for_args()`:

```python
def _workspace_for_args(args) -> Path:
    if args.command in {"run", "skills", "tui"}:
        return args.workspace.expanduser().resolve()
    return Path.cwd().resolve()
```

No change is required here if `chrome-host` is handled before `_workspace_for_args()` runs.

- [ ] **Step 5: Run tests to verify pass**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q
python3.13 -m autonomy chrome-host < /dev/null
```

Expected: pytest PASS; CLI exits `0`.

- [ ] **Step 6: Commit**

```bash
git add autonomy/chrome_host.py autonomy/cli.py tests/test_autonomy_native_chrome_host.py
git commit -m "feat: add chrome native host framing"
```

---

### Task 2: Chrome API Sessions, Chat, Status, and Inspect

**Files:**
- Create: `autonomy/chrome_api.py`
- Modify: `autonomy/chrome_host.py`
- Test: `tests/test_autonomy_native_chrome_host.py`

**Interfaces:**
- Consumes: `ChromeBridge.handle(message: dict[str, Any]) -> dict[str, Any]`
- Produces: `ChromeSessionBridge(workspace: Path | None = None, db_path: Path | None = None, max_steps: int = 12, conversation_factory: Callable[..., Any] | None = None, store_factory: Callable[[Path], Any] = AutonomyStore)`
- Produces: `ChromeSessionBridge.handle(message: dict[str, Any]) -> dict[str, Any]`
- Produces request types: `status`, `session.start`, `chat.send`, `run.inspect`
- Produces response types: `status.result`, `session.started`, `chat.result`, `run.inspect.result`

- [ ] **Step 1: Write failing API tests**

Append to `tests/test_autonomy_native_chrome_host.py`:

```python
import tempfile
from pathlib import Path
from types import SimpleNamespace

from autonomy.models import ConversationResponse, RunResult, TerminationReason


class FakeConversation:
    def __init__(self, *, workspace, db_path, max_steps, agent_loop_factory, responder, store=None, interface="tui"):
        self.workspace = workspace
        self.db_path = db_path
        self.max_steps = max_steps
        self.interface = interface
        self.inputs = []

    def handle_user_input(self, text):
        self.inputs.append(text)
        run_result = RunResult(
            run_id="run-1",
            goal=text,
            termination=TerminationReason.ACHIEVED,
            reason="done",
            steps_executed=1,
            outcome=None,
            learned_items=(),
        )
        return ConversationResponse(
            session_id="session-1",
            user_turn_id="user-1",
            assistant_turn_id="assistant-1",
            run_result=run_result,
            reply="reply text",
            conversation_context="",
            candidate_skills=(),
            action_recipe_candidates=(),
            decision=None,
        )


class FakeStore:
    def __init__(self, db_path):
        self.db_path = db_path

    def inspect_run(self, run_id):
        return {"run_id": run_id, "events": [{"event_type": "run_started"}]}


class AutonomyNativeChromeApiTest(unittest.TestCase):
    def test_chrome_api_starts_session_and_sends_chat(self):
        from autonomy.chrome_api import ChromeSessionBridge

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            bridge = ChromeSessionBridge(
                conversation_factory=FakeConversation,
                store_factory=FakeStore,
            )

            started = bridge.handle(
                {"type": "session.start", "workspace": str(workspace), "max_steps": 3}
            )
            result = bridge.handle(
                {"type": "chat.send", "session_id": started["session_id"], "text": "hello"}
            )

        self.assertTrue(started["ok"])
        self.assertEqual(started["type"], "session.started")
        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "chat.result")
        self.assertEqual(result["reply"], "reply text")
        self.assertEqual(result["run_id"], "run-1")
        self.assertEqual(result["termination"], "achieved")
        self.assertEqual(result["steps_executed"], 1)

    def test_chrome_api_rejects_bad_workspace_and_unknown_session(self):
        from autonomy.chrome_api import ChromeSessionBridge

        bridge = ChromeSessionBridge(conversation_factory=FakeConversation, store_factory=FakeStore)

        bad_workspace = bridge.handle(
            {"type": "session.start", "workspace": "/path/does/not/exist"}
        )
        unknown_session = bridge.handle(
            {"type": "chat.send", "session_id": "missing", "text": "hello"}
        )

        self.assertFalse(bad_workspace["ok"])
        self.assertIn("workspace must exist", bad_workspace["error"])
        self.assertFalse(unknown_session["ok"])
        self.assertIn("unknown session", unknown_session["error"])

    def test_chrome_api_inspects_run(self):
        from autonomy.chrome_api import ChromeSessionBridge

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            bridge = ChromeSessionBridge(
                conversation_factory=FakeConversation,
                store_factory=FakeStore,
            )
            bridge.handle({"type": "session.start", "workspace": str(workspace)})
            result = bridge.handle({"type": "run.inspect", "run_id": "run-1"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "run.inspect.result")
        self.assertEqual(result["run"]["run_id"], "run-1")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py::AutonomyNativeChromeApiTest -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autonomy.chrome_api'`.

- [ ] **Step 3: Add minimal Chrome API bridge**

Create `autonomy/chrome_api.py`:

```python
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

from .cli import build_agent_loop
from .conversation import ConversationLoop
from .conversation_responder import MissingModelConversationResponder, ModelConversationResponder
from .models import jsonable
from .providers import ModelConfigStore, ProviderConfigurationError, create_provider
from .store import AutonomyStore, workspace_db_path


class ChromeSessionBridge:
    def __init__(
        self,
        *,
        conversation_factory: Callable[..., Any] = ConversationLoop,
        store_factory: Callable[[Path], Any] = AutonomyStore,
    ):
        self.conversation_factory = conversation_factory
        self.store_factory = store_factory
        self.sessions: dict[str, Any] = {}
        self.session_stores: dict[str, Any] = {}

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        try:
            request_type = str(message.get("type") or "")
            if request_type == "status":
                return self._status()
            if request_type == "session.start":
                return self._session_start(message)
            if request_type == "chat.send":
                return self._chat_send(message)
            if request_type == "run.inspect":
                return self._run_inspect(message)
            return {"ok": False, "error": "unknown request type"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _status(self) -> dict[str, Any]:
        return {"ok": True, "type": "status.result", "sessions": len(self.sessions)}

    def _session_start(self, message: dict[str, Any]) -> dict[str, Any]:
        workspace = Path(str(message.get("workspace") or "")).expanduser().resolve()
        if not workspace.is_dir():
            return {"ok": False, "error": "workspace must exist and be a directory"}
        max_steps = int(message.get("max_steps") or 12)
        if max_steps < 1:
            return {"ok": False, "error": "max_steps must be at least 1"}
        db_path = workspace_db_path(workspace)
        store = self.store_factory(db_path)
        session_id = uuid.uuid4().hex
        conversation = self.conversation_factory(
            workspace=workspace,
            db_path=db_path,
            max_steps=max_steps,
            agent_loop_factory=build_agent_loop,
            responder=self._responder_for(workspace),
            store=store,
            session_id=session_id,
            interface="chrome",
        )
        self.sessions[session_id] = conversation
        self.session_stores[session_id] = store
        return {
            "ok": True,
            "type": "session.started",
            "session_id": session_id,
            "workspace": str(workspace),
            "max_steps": max_steps,
        }

    def _chat_send(self, message: dict[str, Any]) -> dict[str, Any]:
        session_id = str(message.get("session_id") or "")
        conversation = self.sessions.get(session_id)
        if conversation is None:
            return {"ok": False, "error": "unknown session"}
        text = str(message.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text must not be empty"}
        response = conversation.handle_user_input(text)
        run = response.run_result
        return {
            "ok": True,
            "type": "chat.result",
            "session_id": session_id,
            "reply": response.reply,
            "run_id": run.run_id,
            "termination": run.termination.value,
            "steps_executed": run.steps_executed,
            "reason": run.reason,
        }

    def _run_inspect(self, message: dict[str, Any]) -> dict[str, Any]:
        run_id = str(message.get("run_id") or "").strip()
        if not run_id:
            return {"ok": False, "error": "run_id must not be empty"}
        for store in self.session_stores.values():
            try:
                return {"ok": True, "type": "run.inspect.result", "run": jsonable(store.inspect_run(run_id))}
            except KeyError:
                continue
        return {"ok": False, "error": "unknown run"}

    def _responder_for(self, workspace: Path):
        try:
            config_store = ModelConfigStore(workspace / ".autonomy")
            provider = create_provider(config_store.load(), config_store)
            return ModelConversationResponder(provider)
        except (ProviderConfigurationError, ValueError) as exc:
            return MissingModelConversationResponder(ProviderConfigurationError(str(exc)))
```

- [ ] **Step 4: Wire real API into host**

Modify `autonomy/chrome_host.py`:

```python
def run_chrome_host(
    *,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
    api: ChromeBridge | None = None,
) -> int:
    from .chrome_api import ChromeSessionBridge

    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout.buffer
    api = api or ChromeSessionBridge()
    ...
```

Keep `EchoStatusBridge` only if existing tests still need it. If not needed, delete it by editing with `apply_patch`; do not use `rm`.

- [ ] **Step 5: Run targeted tests**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add autonomy/chrome_api.py autonomy/chrome_host.py tests/test_autonomy_native_chrome_host.py
git commit -m "feat: add chrome conversation bridge"
```

---

### Task 3: Chrome Approval Bridge

**Files:**
- Modify: `autonomy/chrome_api.py`
- Test: `tests/test_autonomy_native_chrome_host.py`

**Interfaces:**
- Consumes: existing `ApprovalPolicy(prompt: Callable[[str], bool])`
- Produces: `ChromeApprovalBroker(send_event: Callable[[dict[str, Any]], None], timeout_seconds: float = 120.0)`
- Produces: `ChromeApprovalBroker.prompt(message: str) -> bool`
- Produces: `ChromeApprovalBroker.respond(approval_id: str, decision: str) -> dict[str, Any]`
- Produces host event: `{ "ok": true, "type": "approval.requested", "approval_id": "...", "message": "..." }`
- Consumes request: `{ "type": "approval.respond", "approval_id": "...", "decision": "allow" | "deny" }`

- [ ] **Step 1: Write failing approval broker tests**

Append to `tests/test_autonomy_native_chrome_host.py`:

```python
import threading
import time


class AutonomyNativeChromeApprovalTest(unittest.TestCase):
    def test_approval_broker_allows_matching_response(self):
        from autonomy.chrome_api import ChromeApprovalBroker

        events = []
        broker = ChromeApprovalBroker(events.append, timeout_seconds=1.0)
        result = {}

        thread = threading.Thread(
            target=lambda: result.update({"allowed": broker.prompt("Approve high-risk action?")})
        )
        thread.start()
        time.sleep(0.05)

        self.assertEqual(events[0]["type"], "approval.requested")
        response = broker.respond(events[0]["approval_id"], "allow")
        thread.join(timeout=1.0)

        self.assertTrue(response["ok"])
        self.assertTrue(result["allowed"])

    def test_approval_broker_denies_timeout_and_bad_decision(self):
        from autonomy.chrome_api import ChromeApprovalBroker

        events = []
        broker = ChromeApprovalBroker(events.append, timeout_seconds=0.01)

        self.assertFalse(broker.prompt("Approve medium-risk action?"))
        self.assertEqual(events[0]["type"], "approval.requested")

        denied = broker.respond("missing", "allow")
        self.assertFalse(denied["ok"])

        broker = ChromeApprovalBroker(events.append, timeout_seconds=1.0)
        result = {}
        thread = threading.Thread(target=lambda: result.update({"allowed": broker.prompt("Approve?")}))
        thread.start()
        time.sleep(0.05)
        broker.respond(events[-1]["approval_id"], "deny")
        thread.join(timeout=1.0)
        self.assertFalse(result["allowed"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py::AutonomyNativeChromeApprovalTest -q
```

Expected: FAIL with `ImportError` for `ChromeApprovalBroker`.

- [ ] **Step 3: Add approval broker**

Modify `autonomy/chrome_api.py`:

```python
import threading
import time


class ChromeApprovalBroker:
    def __init__(
        self,
        send_event: Callable[[dict[str, Any]], None],
        *,
        timeout_seconds: float = 120.0,
    ):
        self.send_event = send_event
        self.timeout_seconds = timeout_seconds
        self._condition = threading.Condition()
        self._pending: dict[str, bool | None] = {}

    def prompt(self, message: str) -> bool:
        approval_id = uuid.uuid4().hex
        with self._condition:
            self._pending[approval_id] = None
        self.send_event(
            {
                "ok": True,
                "type": "approval.requested",
                "approval_id": approval_id,
                "message": self._redact(message),
            }
        )
        deadline = time.monotonic() + self.timeout_seconds
        with self._condition:
            while self._pending.get(approval_id) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pending.pop(approval_id, None)
                    return False
                self._condition.wait(remaining)
            return bool(self._pending.pop(approval_id))

    def respond(self, approval_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"allow", "deny"}:
            return {"ok": False, "error": "decision must be allow or deny"}
        with self._condition:
            if approval_id not in self._pending:
                return {"ok": False, "error": "unknown approval"}
            self._pending[approval_id] = decision == "allow"
            self._condition.notify_all()
        return {
            "ok": True,
            "type": "approval.result",
            "approval_id": approval_id,
            "decision": decision,
        }

    @staticmethod
    def _redact(message: str) -> str:
        return message.replace(".autonomy/.env", ".autonomy/[redacted]")
```

- [ ] **Step 4: Install approval policy into agent loops**

Modify `ChromeSessionBridge.__init__`:

```python
        self.approval_broker = ChromeApprovalBroker(self._send_event)
        self.events: list[dict[str, Any]] = []
```

Add methods:

```python
    def _send_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def pop_events(self) -> list[dict[str, Any]]:
        events = list(self.events)
        self.events.clear()
        return events

    def _agent_loop_factory(self, workspace: Path, db_path: Path):
        from .tools import ApprovalPolicy

        agent_loop = build_agent_loop(workspace, db_path)
        agent_loop.action_gateway.approval = ApprovalPolicy(prompt=self.approval_broker.prompt)
        return agent_loop
```

Use `_agent_loop_factory` in `_session_start()`:

```python
            agent_loop_factory=self._agent_loop_factory,
```

Handle approval responses:

```python
            if request_type == "approval.respond":
                return self.approval_broker.respond(
                    str(message.get("approval_id") or ""),
                    str(message.get("decision") or ""),
                )
```

- [ ] **Step 5: Send queued events from native host**

Modify `autonomy/chrome_host.py` so the host can emit approval events before the final `chat.result`.

Use a writer lock:

```python
import threading


class NativeMessageWriter:
    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self._lock = threading.Lock()

    def send(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            write_native_message(self.stream, payload)
```

In `run_chrome_host()`, after every `api.handle(message)`:

```python
            for event in getattr(api, "pop_events", lambda: [])():
                writer.send(event)
            writer.send(response)
```

This keeps one final response per request and permits extra host events for approval prompts.

- [ ] **Step 6: Run approval tests**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py::AutonomyNativeChromeApprovalTest -q
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add autonomy/chrome_api.py autonomy/chrome_host.py tests/test_autonomy_native_chrome_host.py
git commit -m "feat: add chrome approval bridge"
```

---

### Task 4: Chrome Extension Side Panel

**Files:**
- Create: `chrome-extension/manifest.json`
- Create: `chrome-extension/service_worker.js`
- Create: `chrome-extension/sidepanel.html`
- Create: `chrome-extension/sidepanel.js`
- Create: `chrome-extension/sidepanel.css`
- Create: `chrome-extension/native-host.example.json`
- Test: `tests/test_chrome_extension_static.py`

**Interfaces:**
- Consumes host request types: `status`, `session.start`, `chat.send`, `run.inspect`, `approval.respond`
- Consumes host event type: `approval.requested`
- Produces side panel messages to service worker: `{ type: string, ...payload }`
- Produces service worker bridge through `chrome.runtime.connectNative("com.autonomy.app")`

- [ ] **Step 1: Write failing static extension tests**

Create `tests/test_chrome_extension_static.py`:

```python
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "chrome-extension"


class ChromeExtensionStaticTest(unittest.TestCase):
    def test_manifest_declares_mv3_side_panel_and_native_messaging(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["name"], "Autonomy")
        self.assertIn("nativeMessaging", manifest["permissions"])
        self.assertIn("sidePanel", manifest["permissions"])
        self.assertEqual(manifest["side_panel"]["default_path"], "sidepanel.html")
        self.assertEqual(manifest["background"]["service_worker"], "service_worker.js")

    def test_extension_files_reference_required_message_types(self):
        service_worker = (EXTENSION / "service_worker.js").read_text(encoding="utf-8")
        sidepanel = (EXTENSION / "sidepanel.js").read_text(encoding="utf-8")
        html = (EXTENSION / "sidepanel.html").read_text(encoding="utf-8")

        for message_type in (
            "status",
            "session.start",
            "chat.send",
            "run.inspect",
            "approval.respond",
            "approval.requested",
        ):
            self.assertIn(message_type, service_worker + sidepanel)
        self.assertIn('id="workspace"', html)
        self.assertIn('id="approval-modal"', html)

    def test_native_host_example_restricts_extension_origin(self):
        manifest = json.loads((EXTENSION / "native-host.example.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "com.autonomy.app")
        self.assertEqual(manifest["type"], "stdio")
        self.assertEqual(manifest["allowed_origins"], ["chrome-extension://EXTENSION_ID/"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3.13 -m pytest tests/test_chrome_extension_static.py -q
```

Expected: FAIL because `chrome-extension/manifest.json` is missing.

- [ ] **Step 3: Add Manifest V3 files**

Create `chrome-extension/manifest.json`:

```json
{
  "manifest_version": 3,
  "name": "Autonomy",
  "version": "0.1.0",
  "description": "Chrome side panel UI for local Autonomy.",
  "permissions": ["nativeMessaging", "sidePanel", "storage"],
  "background": {
    "service_worker": "service_worker.js"
  },
  "side_panel": {
    "default_path": "sidepanel.html"
  },
  "action": {
    "default_title": "Autonomy"
  }
}
```

Create `chrome-extension/native-host.example.json`:

```json
{
  "name": "com.autonomy.app",
  "description": "Autonomy Chrome native messaging host",
  "path": "/absolute/path/to/autonomy",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://EXTENSION_ID/"]
}
```

- [ ] **Step 4: Add service worker**

Create `chrome-extension/service_worker.js`:

```javascript
let nativePort = null;
const panelPorts = new Set();

function connectNative() {
  if (nativePort) return nativePort;
  nativePort = chrome.runtime.connectNative("com.autonomy.app");
  nativePort.onMessage.addListener((message) => {
    for (const port of panelPorts) port.postMessage(message);
  });
  nativePort.onDisconnect.addListener(() => {
    nativePort = null;
    for (const port of panelPorts) {
      port.postMessage({ ok: false, error: "Autonomy native host disconnected" });
    }
  });
  return nativePort;
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "autonomy-panel") return;
  panelPorts.add(port);
  port.onDisconnect.addListener(() => panelPorts.delete(port));
  port.onMessage.addListener((message) => {
    const type = message && message.type;
    if (
      type !== "status" &&
      type !== "session.start" &&
      type !== "chat.send" &&
      type !== "run.inspect" &&
      type !== "approval.respond"
    ) {
      port.postMessage({ ok: false, error: "unknown panel message type" });
      return;
    }
    connectNative().postMessage(message);
  });
});
```

- [ ] **Step 5: Add side panel HTML**

Create `chrome-extension/sidepanel.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Autonomy</title>
    <link rel="stylesheet" href="sidepanel.css" />
  </head>
  <body>
    <header>
      <h1>Autonomy</h1>
      <button id="status">Status</button>
    </header>
    <section id="setup">
      <input id="workspace" type="text" placeholder="/path/to/workspace" />
      <input id="max-steps" type="number" min="1" value="12" />
      <button id="start-session">Start</button>
    </section>
    <main id="messages" aria-live="polite"></main>
    <section id="composer">
      <textarea id="prompt" rows="4" placeholder="Ask Autonomy"></textarea>
      <button id="send">Send</button>
    </section>
    <dialog id="approval-modal">
      <form method="dialog">
        <h2>Approval Required</h2>
        <pre id="approval-message"></pre>
        <button id="approval-deny" value="deny">Deny</button>
        <button id="approval-allow" value="allow">Allow</button>
      </form>
    </dialog>
    <script src="sidepanel.js"></script>
  </body>
</html>
```

- [ ] **Step 6: Add side panel JavaScript**

Create `chrome-extension/sidepanel.js`:

```javascript
const port = chrome.runtime.connect({ name: "autonomy-panel" });
let sessionId = "";
let pendingApprovalId = "";

const messages = document.getElementById("messages");
const workspace = document.getElementById("workspace");
const maxSteps = document.getElementById("max-steps");
const promptBox = document.getElementById("prompt");
const approvalModal = document.getElementById("approval-modal");
const approvalMessage = document.getElementById("approval-message");

function append(role, text) {
  const item = document.createElement("article");
  item.className = role;
  item.textContent = text;
  messages.appendChild(item);
  messages.scrollTop = messages.scrollHeight;
}

function send(message) {
  port.postMessage(message);
}

port.onMessage.addListener((message) => {
  if (!message.ok) {
    append("error", message.error || "Unknown error");
    return;
  }
  if (message.type === "status.result") {
    append("system", `status: ${JSON.stringify(message)}`);
  } else if (message.type === "session.started") {
    sessionId = message.session_id;
    append("system", `session: ${sessionId}`);
  } else if (message.type === "chat.result") {
    append("assistant", `${message.reply}\nrun_id=${message.run_id} termination=${message.termination}`);
  } else if (message.type === "run.inspect.result") {
    append("system", JSON.stringify(message.run, null, 2));
  } else if (message.type === "approval.requested") {
    pendingApprovalId = message.approval_id;
    approvalMessage.textContent = message.message;
    approvalModal.showModal();
  }
});

document.getElementById("status").addEventListener("click", () => {
  send({ type: "status" });
});

document.getElementById("start-session").addEventListener("click", () => {
  send({
    type: "session.start",
    workspace: workspace.value,
    max_steps: Number(maxSteps.value || 12),
  });
});

document.getElementById("send").addEventListener("click", () => {
  const text = promptBox.value.trim();
  if (!sessionId || !text) return;
  append("user", text);
  promptBox.value = "";
  send({ type: "chat.send", session_id: sessionId, text });
});

document.getElementById("approval-allow").addEventListener("click", () => {
  send({ type: "approval.respond", approval_id: pendingApprovalId, decision: "allow" });
});

document.getElementById("approval-deny").addEventListener("click", () => {
  send({ type: "approval.respond", approval_id: pendingApprovalId, decision: "deny" });
});
```

- [ ] **Step 7: Add side panel CSS**

Create `chrome-extension/sidepanel.css`:

```css
* {
  box-sizing: border-box;
}

body {
  margin: 0;
  font: 14px system-ui, sans-serif;
  color: #111827;
  background: #f9fafb;
}

header,
#setup,
#composer {
  display: grid;
  gap: 8px;
  padding: 10px;
  border-bottom: 1px solid #e5e7eb;
  background: white;
}

header {
  grid-template-columns: 1fr auto;
  align-items: center;
}

h1 {
  margin: 0;
  font-size: 16px;
}

input,
textarea,
button {
  width: 100%;
  font: inherit;
}

button {
  min-height: 32px;
}

#messages {
  display: grid;
  gap: 8px;
  height: calc(100vh - 260px);
  min-height: 220px;
  overflow: auto;
  padding: 10px;
}

article {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px;
  background: white;
}

.user {
  background: #eff6ff;
}

.assistant {
  background: #ecfdf5;
}

.error {
  background: #fef2f2;
  border-color: #fecaca;
}

dialog {
  width: min(92vw, 360px);
  border: 1px solid #d1d5db;
  border-radius: 8px;
}

pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
```

- [ ] **Step 8: Run static tests**

Run:

```bash
python3.13 -m pytest tests/test_chrome_extension_static.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add chrome-extension tests/test_chrome_extension_static.py
git commit -m "feat: add chrome side panel extension"
```

---

### Task 5: Documentation, End-to-End Verification, and Safe Ship Review

**Files:**
- Modify: `README.md`
- Test: existing test suite

**Interfaces:**
- Consumes: `autonomy chrome-host`
- Consumes: `chrome-extension/native-host.example.json`
- Produces README section: Chrome extension setup and dev loading steps

- [ ] **Step 1: Write README packaging test**

Append to `tests/test_autonomy_packaging.py`:

```python
def test_readme_documents_chrome_extension_native_host():
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "autonomy chrome-host" in text
    assert "chrome-extension" in text
    assert "native-host.example.json" in text
    assert "com.autonomy.app" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_packaging.py::test_readme_documents_chrome_extension_native_host -q
```

Expected: FAIL because README does not document Chrome extension setup.

- [ ] **Step 3: Add README section**

Add this section to `README.md` near commands or UI docs:

```markdown
## Chrome Extension UI

Autonomy includes a local Chrome side panel UI for development.

The extension does not execute tools directly. It talks to the local native
messaging host, and the host routes requests into:

```text
ConversationLoop(interface="chrome") -> AgentLoop -> ActionGateway -> ToolRegistry
```

Host command:

```bash
autonomy chrome-host
```

Development setup:

1. Install this checkout in your active Python environment.
2. Load `chrome-extension/` as an unpacked extension in Chrome.
3. Copy `chrome-extension/native-host.example.json` to Chrome's native messaging host directory.
4. Replace `EXTENSION_ID` with the unpacked extension ID.
5. Replace `path` with the absolute path to the `autonomy` executable from your environment.

Native host name:

```text
com.autonomy.app
```

The native host manifest restricts access to the configured extension origin.
The extension never receives provider API keys or `.autonomy/.env` content.
Approval prompts default to deny on timeout or disconnect.
```

- [ ] **Step 4: Run full targeted verification**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q
python3.13 -m pytest tests/test_chrome_extension_static.py -q
python3.13 -m pytest tests/test_autonomy_native_cli.py -q
python3.13 -m pytest tests/test_autonomy_packaging.py -q
python3.13 -m autonomy chrome-host < /dev/null
git diff --check
```

Expected: all pass.

- [ ] **Step 5: Run full suite**

Run:

```bash
python3.13 -m pytest -q
```

Expected: full suite pass.

- [ ] **Step 6: Review final diff for scope**

Run:

```bash
git diff --stat
git diff -- README.md autonomy/chrome_api.py autonomy/chrome_host.py autonomy/cli.py tests/test_autonomy_native_chrome_host.py tests/test_chrome_extension_static.py tests/test_autonomy_packaging.py chrome-extension/manifest.json chrome-extension/service_worker.js chrome-extension/sidepanel.html chrome-extension/sidepanel.js chrome-extension/sidepanel.css chrome-extension/native-host.example.json
```

Expected: only Chrome host, Chrome extension, CLI wiring, tests, and README changed.

- [ ] **Step 7: Commit**

```bash
git add README.md autonomy/chrome_api.py autonomy/chrome_host.py autonomy/cli.py tests/test_autonomy_native_chrome_host.py tests/test_chrome_extension_static.py tests/test_autonomy_packaging.py chrome-extension
git commit -m "feat: add chrome extension bridge"
```

---

## Final Verification

Run after all tasks:

```bash
python3.13 -m pytest tests/test_autonomy_native_chrome_host.py -q
python3.13 -m pytest tests/test_chrome_extension_static.py -q
python3.13 -m pytest tests/test_autonomy_native_cli.py -q
python3.13 -m pytest tests/test_autonomy_packaging.py -q
python3.13 -m pytest -q
python3.13 -m autonomy chrome-host < /dev/null
git diff --check
git status --short
```

Expected:

- Chrome host tests pass.
- Extension static tests pass.
- CLI and packaging tests pass.
- Full pytest suite passes.
- `autonomy chrome-host` exits cleanly on EOF.
- Diff hygiene clean.
- Worktree contains only intended committed changes or is clean after final commit.

## Self-Review Notes

- Spec coverage: native host, side panel, status, session, chat, inspect, approval modal, safety validation, and docs are covered by Tasks 1-5.
- Dependency check: no new Python or JavaScript packages.
- Boundary check: extension talks only to native host; backend still routes through `ConversationLoop` and existing `ApprovalPolicy`.
- Scope check: no localhost server, remote API, DOM automation, or Chrome Web Store packaging.
