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


DISCORD_MESSAGE_LIMIT = 1_900


class DiscordConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class DiscordBotConfig:
    token: str
    owner_id: int


SendApproval = Callable[[str, str], Awaitable[None]]


def load_discord_config(workspace: Path) -> DiscordBotConfig:
    config_store = ModelConfigStore(workspace_autonomy_home(workspace))
    if config_store.env_permissions_secure() is False:
        raise DiscordConfigurationError(f"Discord secrets file must have mode 0600: {config_store.env_path}")
    secrets = config_store._read_secrets()
    token = secrets.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise DiscordConfigurationError(f"DISCORD_BOT_TOKEN is missing from {config_store.env_path}")
    raw_owner_id = secrets.get("DISCORD_OWNER_ID", "").strip()
    if not raw_owner_id:
        raise DiscordConfigurationError(f"DISCORD_OWNER_ID is missing from {config_store.env_path}")
    try:
        owner_id = int(raw_owner_id)
    except ValueError as exc:
        raise DiscordConfigurationError("DISCORD_OWNER_ID must be an integer") from exc
    if owner_id < 1:
        raise DiscordConfigurationError("DISCORD_OWNER_ID must be a positive integer")
    return DiscordBotConfig(token=token, owner_id=owner_id)


def _discord_message(text: str) -> str:
    text = text.strip() or "(empty response)"
    if len(text) <= DISCORD_MESSAGE_LIMIT:
        return text
    suffix = "\n[truncated]"
    return text[: DISCORD_MESSAGE_LIMIT - len(suffix)] + suffix


def _redact(message: str) -> str:
    return message.replace(".autonomy/.env", ".autonomy/[redacted]")


class DiscordApprovalBroker:
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


class DiscordSessionBridge:
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
        self.approval_broker = DiscordApprovalBroker(
            owner_id,
            send_approval or self._noop_approval,
            timeout_seconds=approval_timeout_seconds,
        )

    @property
    def session_count(self) -> int:
        return 1 if self.conversation is not None else 0

    async def handle_message(self, *, author_id: int, content: str) -> list[str]:
        self.approval_broker.attach_loop(asyncio.get_running_loop())
        if author_id != self.owner_id:
            return ["This Autonomy Discord bot is owner-only."]
        text = content.strip()
        if not text:
            return []
        if text == "!status":
            return [self._status()]
        if text.startswith("!inspect"):
            return [self._inspect(text)]
        if text == "!reset":
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
            return [_discord_message(message)]
        except Exception as exc:
            return [_discord_message(str(exc))]
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
            interface="discord",
        )

    def _status(self) -> str:
        try:
            configuration = ModelConfigStore(workspace_autonomy_home(self.workspace)).load()
            model = f"{configuration.provider}/{configuration.model}"
        except (ProviderConfigurationError, ValueError) as exc:
            model = f"not configured ({exc})"
        return _discord_message(
            f"status: discord connected; sessions={self.session_count}; "
            f"workspace={self.workspace}; max_steps={self.max_steps}; model={model}"
        )

    def _inspect(self, text: str) -> str:
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            return "usage: !inspect RUN_ID"
        if self.store is None:
            return "unknown run"
        try:
            payload = self.store.inspect_run(parts[1].strip())
        except KeyError:
            return "unknown run"
        return _discord_message(json.dumps(jsonable(payload), indent=2, sort_keys=True))

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


def _import_discord():
    import discord  # type: ignore[import-not-found]

    return discord


def run_discord_bot(
    *,
    workspace: Path,
    db_path: Path | None = None,
    max_steps: int = 12,
    import_discord: Callable[[], Any] = _import_discord,
    output=sys.stderr,
) -> int:
    try:
        config = load_discord_config(workspace)
        discord = import_discord()
    except ImportError:
        print('Discord support is not installed; run `python -m pip install -e ".[discord]"`.', file=output)
        return 2
    except (DiscordConfigurationError, ProviderConfigurationError, ValueError) as exc:
        print(f"discord bot error: {exc}", file=output)
        return 2

    client_holder: dict[str, Any] = {}

    async def send_approval(approval_id: str, message: str) -> None:
        client = client_holder["client"]
        owner = client.get_user(config.owner_id) or await client.fetch_user(config.owner_id)
        view = _approval_view(discord, bridge, config.owner_id, approval_id)
        await owner.send(_discord_message(message), view=view)

    try:
        bridge = DiscordSessionBridge(
            workspace=workspace,
            db_path=db_path,
            owner_id=config.owner_id,
            max_steps=max_steps,
            send_approval=send_approval,
        )
    except ValueError as exc:
        print(f"discord bot error: {exc}", file=output)
        return 2

    intents = discord.Intents.default()
    intents.dm_messages = True
    intents.message_content = True

    class AutonomyDiscordClient(discord.Client):
        async def on_ready(self):
            print(f"Autonomy Discord bot connected as {self.user}", file=output)

        async def on_message(self, message):
            if getattr(message.author, "bot", False) or getattr(message, "guild", None) is not None:
                return
            replies = await bridge.handle_message(
                author_id=int(message.author.id),
                content=str(getattr(message, "content", "")),
            )
            for reply in replies:
                await message.channel.send(reply)

        async def close(self):
            bridge.approval_broker.deny_all()
            await super().close()

    client = AutonomyDiscordClient(intents=intents)
    client_holder["client"] = client
    client.run(config.token)
    return 0


def _approval_view(discord, bridge: DiscordSessionBridge, owner_id: int, approval_id: str):
    class ApprovalView(discord.ui.View):
        @discord.ui.button(label="Allow", style=discord.ButtonStyle.success)
        async def allow(self, interaction, button):
            del button
            await self._respond(interaction, "allow")

        @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
        async def deny(self, interaction, button):
            del button
            await self._respond(interaction, "deny")

        async def _respond(self, interaction, decision: str) -> None:
            if int(interaction.user.id) != owner_id:
                await interaction.response.send_message("Only the owner can respond.")
                return
            result = bridge.handle_approval(
                author_id=owner_id,
                approval_id=approval_id,
                decision=decision,
            )
            await interaction.response.send_message(
                f"approval {result.get('decision')}" if result.get("ok") else str(result.get("error"))
            )
            self.stop()

    return ApprovalView(timeout=120)
