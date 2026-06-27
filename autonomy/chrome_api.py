from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .cli import build_agent_loop
from .conversation import ConversationLoop
from .conversation_responder import MissingModelConversationResponder, ModelConversationResponder
from .model import AutonomyModel
from .models import jsonable
from .providers import ModelConfigStore, ProviderConfigurationError, create_provider
from .storage import workspace_autonomy_home, workspace_db_path
from .store import AutonomyStore
from .tools import ApprovalPolicy


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

    def deny_all(self, reason: str = "approval denied by disconnect") -> int:
        del reason
        with self._condition:
            pending_ids = [approval_id for approval_id, decision in self._pending.items() if decision is None]
            for approval_id in pending_ids:
                self._pending[approval_id] = False
            if pending_ids:
                self._condition.notify_all()
            return len(pending_ids)

    @staticmethod
    def _redact(message: str) -> str:
        return message.replace(".autonomy/.env", ".autonomy/[redacted]")


class ChromeSessionBridge:
    def __init__(
        self,
        workspace: Path | None = None,
        db_path: Path | None = None,
        max_steps: int = 12,
        *,
        conversation_factory: Callable[..., Any] = ConversationLoop,
        store_factory: Callable[[Path], Any] = AutonomyStore,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.workspace = workspace
        self.db_path = db_path
        self.max_steps = max_steps
        self.conversation_factory = conversation_factory
        self.store_factory = store_factory
        self.sessions: dict[str, Any] = {}
        self.session_stores: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.event_sink: Callable[[dict[str, Any]], None] | None = None
        self.approval_broker = ChromeApprovalBroker(self._send_event)
        self._busy_sessions: set[str] = set()
        self._busy_lock = threading.Lock()

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        request_type = str(message.get("type") or "")
        try:
            if request_type == "status":
                return self._status()
            if request_type == "session.start":
                return self._session_start(message)
            if request_type == "chat.send":
                return self._chat_send(message)
            if request_type == "run.inspect":
                return self._run_inspect(message)
            if request_type == "approval.respond":
                return self.approval_broker.respond(
                    str(message.get("approval_id") or ""),
                    str(message.get("decision") or ""),
                )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": "unknown request type"}

    def _status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "type": "status.result",
            "sessions": len(self.sessions),
        }

    def _session_start(self, message: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace_from(message)
        max_steps = self._max_steps_from(message)
        db_path = self._db_path_for(workspace)
        store = self.store_factory(db_path)
        session_id = uuid.uuid4().hex
        conversation = self.conversation_factory(
            workspace=workspace,
            db_path=db_path,
            max_steps=max_steps,
            agent_loop_factory=self._agent_loop_factory,
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
        with self._busy_lock:
            if session_id in self._busy_sessions:
                return {"ok": False, "error": "session is busy"}
            self._busy_sessions.add(session_id)
        try:
            response = conversation.handle_user_input(text)
            run = response.run_result
            if run is None:
                return {"ok": False, "error": "missing run result"}
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
        finally:
            with self._busy_lock:
                self._busy_sessions.discard(session_id)

    def _run_inspect(self, message: dict[str, Any]) -> dict[str, Any]:
        run_id = str(message.get("run_id") or "").strip()
        if not run_id:
            return {"ok": False, "error": "run_id must not be empty"}
        for store in self.session_stores.values():
            try:
                payload = store.inspect_run(run_id)
            except KeyError:
                continue
            return {
                "ok": True,
                "type": "run.inspect.result",
                "run": jsonable(payload),
            }
        return {"ok": False, "error": "unknown run"}

    def pop_events(self) -> list[dict[str, Any]]:
        events = list(self.events)
        self.events.clear()
        return events

    def set_event_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        self.event_sink = sink

    def deny_pending_approvals(self, reason: str = "approval denied by disconnect") -> int:
        return self.approval_broker.deny_all(reason)

    def _workspace_from(self, message: dict[str, Any]) -> Path:
        raw_workspace = message.get("workspace")
        if raw_workspace in (None, ""):
            if self.workspace is None:
                raise ValueError("workspace must exist and be a directory")
            workspace = self.workspace.expanduser().resolve()
        else:
            workspace = Path(str(raw_workspace)).expanduser().resolve()
        if not workspace.exists() or not workspace.is_dir():
            raise ValueError("workspace must exist and be a directory")
        return workspace

    def _max_steps_from(self, message: dict[str, Any]) -> int:
        max_steps = int(message.get("max_steps") or self.max_steps)
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        return max_steps

    def _db_path_for(self, workspace: Path) -> Path:
        if self.db_path is not None:
            return self.db_path.expanduser()
        return workspace_db_path(workspace)

    def _send_event(self, event: dict[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(event)
            return
        self.events.append(event)

    def _agent_loop_factory(self, workspace: Path, db_path: Path):
        agent_loop = build_agent_loop(workspace, db_path)
        agent_loop.action_gateway.approval = ApprovalPolicy(prompt=self.approval_broker.prompt)
        return agent_loop

    def _responder_for(self, workspace: Path):
        try:
            config_dir = workspace_autonomy_home(workspace)
            config_store = ModelConfigStore(config_dir)
            provider = create_provider(config_store.load(), config_store)
            model = AutonomyModel.from_provider(provider)
            return ModelConversationResponder(model)
        except (ProviderConfigurationError, ValueError) as exc:
            return MissingModelConversationResponder(ProviderConfigurationError(str(exc)))
