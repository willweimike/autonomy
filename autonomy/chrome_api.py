from __future__ import annotations

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

    def _responder_for(self, workspace: Path):
        try:
            config_dir = workspace_autonomy_home(workspace)
            config_store = ModelConfigStore(config_dir)
            provider = create_provider(config_store.load(), config_store)
            model = AutonomyModel.from_provider(provider)
            return ModelConversationResponder(model)
        except (ProviderConfigurationError, ValueError) as exc:
            return MissingModelConversationResponder(ProviderConfigurationError(str(exc)))
