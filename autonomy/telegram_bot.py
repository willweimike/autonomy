from __future__ import annotations

import asyncio
import json
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cli import build_agent_loop
from .conversation import ConversationLoop
from .conversation_responder import MissingModelConversationResponder, ModelConversationResponder
from .model import AutonomyModel
from .models import jsonable
from .providers import ModelConfigStore, ProviderConfigurationError, create_provider
from .storage import workspace_autonomy_home, workspace_db_path
from .store import AutonomyStore
from .tools import ApprovalPolicy


TELEGRAM_MESSAGE_LIMIT = 3_900


class TelegramConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramBotConfig:
    token: str
    owner_id: int


SendApproval = Callable[[str, str], Awaitable[None]]


def load_telegram_config(workspace: Path) -> TelegramBotConfig:
    config_store = ModelConfigStore(workspace_autonomy_home(workspace))
    if config_store.env_permissions_secure() is False:
        raise TelegramConfigurationError(f"Telegram secrets file must have mode 0600: {config_store.env_path}")
    secrets = config_store._read_secrets()
    token = secrets.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise TelegramConfigurationError(f"TELEGRAM_BOT_TOKEN is missing from {config_store.env_path}")
    raw_owner_id = secrets.get("TELEGRAM_OWNER_ID", "").strip()
    if not raw_owner_id:
        raise TelegramConfigurationError(f"TELEGRAM_OWNER_ID is missing from {config_store.env_path}")
    try:
        owner_id = int(raw_owner_id)
    except ValueError as exc:
        raise TelegramConfigurationError("TELEGRAM_OWNER_ID must be an integer") from exc
    if owner_id < 1:
        raise TelegramConfigurationError("TELEGRAM_OWNER_ID must be a positive integer")
    return TelegramBotConfig(token=token, owner_id=owner_id)


def _telegram_message(text: str) -> str:
    text = text.strip() or "(empty response)"
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    suffix = "\n[truncated]"
    return text[: TELEGRAM_MESSAGE_LIMIT - len(suffix)] + suffix


def _redact(message: str) -> str:
    return message.replace(".autonomy/.env", ".autonomy/[redacted]")


class TelegramApprovalBroker:
    def __init__(
        self,
        owner_id: int,
        send_approval: SendApproval,
        *,
        timeout_seconds: float = 120.0,
    ):
        self.owner_id = owner_id
        self.send_approval = send_approval
        self.timeout_seconds = timeout_seconds
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._pending: dict[str, asyncio.Future[bool]] = {}

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def prompt(self, message: str) -> bool:
        if self.loop is None:
            return False
        future = asyncio.run_coroutine_threadsafe(self._prompt(message), self.loop)
        try:
            return bool(future.result(timeout=self.timeout_seconds + 1.0))
        except Exception:
            return False

    async def _prompt(self, message: str) -> bool:
        approval_id = uuid.uuid4().hex
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        with self._lock:
            self._pending[approval_id] = future
        try:
            await asyncio.wait_for(
                self.send_approval(approval_id, _redact(message)),
                timeout=self.timeout_seconds,
            )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            return bool(await asyncio.wait_for(future, timeout=remaining))
        except (asyncio.TimeoutError, Exception):
            return False
        finally:
            with self._lock:
                self._pending.pop(approval_id, None)

    def respond(self, author_id: int, approval_id: str, decision: str) -> dict[str, Any]:
        if author_id != self.owner_id:
            return {"ok": False, "error": "approval is owner-only"}
        if decision not in {"allow", "deny"}:
            return {"ok": False, "error": "decision must be allow or deny"}
        with self._lock:
            future = self._pending.get(approval_id)
        if future is None:
            return {"ok": False, "error": "unknown approval"}
        if not future.done():
            future.set_result(decision == "allow")
        return {"ok": True, "approval_id": approval_id, "decision": decision}

    def deny_all(self) -> int:
        with self._lock:
            pending = list(self._pending.values())
        for future in pending:
            if not future.done():
                future.set_result(False)
        return len(pending)


class TelegramSessionBridge:
    def __init__(
        self,
        *,
        workspace: Path,
        db_path: Path | None = None,
        owner_id: int,
        max_steps: int = 12,
        send_approval: SendApproval | None = None,
        approval_timeout_seconds: float = 120.0,
        conversation_factory: Callable[..., Any] = ConversationLoop,
        store_factory: Callable[[Path], Any] = AutonomyStore,
    ):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self.workspace = workspace.expanduser().resolve()
        if not self.workspace.exists() or not self.workspace.is_dir():
            raise ValueError("workspace must exist and be a directory")
        self.db_path = db_path.expanduser() if db_path else workspace_db_path(self.workspace)
        self.owner_id = owner_id
        self.max_steps = max_steps
        self.conversation_factory = conversation_factory
        self.store_factory = store_factory
        self.session_id = ""
        self.conversation: Any | None = None
        self.store: Any | None = None
        self._busy = False
        self._busy_lock = asyncio.Lock()
        self.approval_broker = TelegramApprovalBroker(
            owner_id,
            send_approval or self._noop_approval,
            timeout_seconds=approval_timeout_seconds,
        )

    @property
    def session_count(self) -> int:
        return 1 if self.conversation is not None else 0

    async def handle_message(self, *, author_id: int, content: str, chat_type: str = "private") -> list[str]:
        self.approval_broker.attach_loop(asyncio.get_running_loop())
        if chat_type != "private":
            return []
        if author_id != self.owner_id:
            return ["This Autonomy Telegram bot is owner-only."]
        text = content.strip()
        if not text:
            return []
        command = text.split(maxsplit=1)[0].split("@", 1)[0]
        if command == "/status":
            return [self._status()]
        if command == "/inspect":
            return [self._inspect(text)]
        if command == "/reset":
            async with self._busy_lock:
                if self._busy:
                    return ["session is busy"]
                self.approval_broker.deny_all()
                self._start_session()
                return [f"session reset: {self.session_id}"]
        return await self._chat(text)

    def handle_approval(self, *, author_id: int, approval_id: str, decision: str) -> dict[str, Any]:
        return self.approval_broker.respond(author_id, approval_id, decision)

    async def _chat(self, text: str) -> list[str]:
        async with self._busy_lock:
            if self._busy:
                return ["session is busy"]
            self._busy = True
        try:
            if self.conversation is None:
                self._start_session()
            response = await asyncio.to_thread(self.conversation.handle_user_input, text)
            run = response.run_result
            if run is None:
                return ["missing run result"]
            message = "\n".join(
                [
                    str(response.reply),
                    "",
                    f"run_id={run.run_id}",
                    f"termination={run.termination.value}",
                    f"steps={run.steps_executed}",
                ]
            )
            return [_telegram_message(message)]
        except Exception as exc:
            return [_telegram_message(str(exc))]
        finally:
            async with self._busy_lock:
                self._busy = False

    def _start_session(self) -> None:
        self.session_id = uuid.uuid4().hex
        self.store = self.store_factory(self.db_path)
        self.conversation = self.conversation_factory(
            workspace=self.workspace,
            db_path=self.db_path,
            max_steps=self.max_steps,
            agent_loop_factory=self._agent_loop_factory,
            responder=self._responder_for(),
            store=self.store,
            session_id=self.session_id,
            interface="telegram",
        )

    def _status(self) -> str:
        try:
            configuration = ModelConfigStore(workspace_autonomy_home(self.workspace)).load()
            model = f"{configuration.provider}/{configuration.model}"
        except (ProviderConfigurationError, ValueError) as exc:
            model = f"not configured ({exc})"
        return _telegram_message(
            f"status: telegram connected; sessions={self.session_count}; "
            f"workspace={self.workspace}; max_steps={self.max_steps}; model={model}"
        )

    def _inspect(self, text: str) -> str:
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            return "usage: /inspect RUN_ID"
        if self.store is None:
            return "unknown run"
        try:
            payload = self.store.inspect_run(parts[1].strip())
        except KeyError:
            return "unknown run"
        return _telegram_message(json.dumps(jsonable(payload), indent=2, sort_keys=True))

    def _agent_loop_factory(self, workspace: Path, db_path: Path):
        agent_loop = build_agent_loop(workspace, db_path)
        agent_loop.action_gateway.approval = ApprovalPolicy(prompt=self.approval_broker.prompt)
        return agent_loop

    def _responder_for(self):
        try:
            config_dir = workspace_autonomy_home(self.workspace)
            config_store = ModelConfigStore(config_dir)
            provider = create_provider(config_store.load(), config_store)
            model = AutonomyModel.from_provider(provider)
            return ModelConversationResponder(model)
        except (ProviderConfigurationError, ValueError) as exc:
            return MissingModelConversationResponder(ProviderConfigurationError(str(exc)))

    @staticmethod
    async def _noop_approval(approval_id: str, message: str) -> None:
        del approval_id, message


def _import_telegram():
    import telegram  # type: ignore[import-not-found]
    import telegram.ext as telegram_ext  # type: ignore[import-not-found]

    return telegram, telegram_ext


def run_telegram_bot(
    *,
    workspace: Path,
    db_path: Path | None = None,
    max_steps: int = 12,
    import_telegram: Callable[[], Any] = _import_telegram,
    output=sys.stderr,
) -> int:
    try:
        config = load_telegram_config(workspace)
        telegram, telegram_ext = import_telegram()
    except ImportError:
        print('Telegram support is not installed; run `python -m pip install -e ".[telegram]"`.', file=output)
        return 2
    except (TelegramConfigurationError, ProviderConfigurationError, ValueError) as exc:
        print(f"telegram bot error: {exc}", file=output)
        return 2

    async def send_approval(approval_id: str, message: str) -> None:
        keyboard = telegram.InlineKeyboardMarkup(
            [
                [
                    telegram.InlineKeyboardButton("Allow", callback_data=f"approval:{approval_id}:allow"),
                    telegram.InlineKeyboardButton("Deny", callback_data=f"approval:{approval_id}:deny"),
                ]
            ]
        )
        await application.bot.send_message(
            chat_id=config.owner_id,
            text=_telegram_message(message),
            reply_markup=keyboard,
        )

    try:
        bridge = TelegramSessionBridge(
            workspace=workspace,
            db_path=db_path,
            owner_id=config.owner_id,
            max_steps=max_steps,
            send_approval=send_approval,
        )
    except ValueError as exc:
        print(f"telegram bot error: {exc}", file=output)
        return 2

    async def handle_text(update, context) -> None:
        del context
        message = getattr(update, "effective_message", None)
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        if message is None or user is None or chat is None:
            return
        replies = await bridge.handle_message(
            author_id=int(user.id),
            content=str(getattr(message, "text", "") or ""),
            chat_type=str(getattr(chat, "type", "")),
        )
        for reply in replies:
            await message.reply_text(reply)

    async def handle_callback(update, context) -> None:
        del context
        query = getattr(update, "callback_query", None)
        user = getattr(update, "effective_user", None)
        if query is None or user is None:
            return
        data = str(getattr(query, "data", "") or "")
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[0] != "approval":
            await query.answer("unknown approval")
            return
        result = bridge.handle_approval(
            author_id=int(user.id),
            approval_id=parts[1],
            decision=parts[2],
        )
        await query.answer(f"approval {result.get('decision')}" if result.get("ok") else str(result.get("error")))

    application = telegram_ext.ApplicationBuilder().token(config.token).build()
    application.add_handler(telegram_ext.CallbackQueryHandler(handle_callback, pattern=r"^approval:"))
    application.add_handler(telegram_ext.MessageHandler(telegram_ext.filters.TEXT, handle_text))
    try:
        print("Autonomy Telegram bot polling", file=output)
        application.run_polling()
    finally:
        bridge.approval_broker.deny_all()
    return 0
