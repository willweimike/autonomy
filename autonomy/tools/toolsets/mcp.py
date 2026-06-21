from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import threading
from collections.abc import Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ...models import Observation, RiskLevel
from ..redaction import redact_sensitive_text
from ..registry import ToolRegistry

MCP_CONFIG_NAME = "mcp_servers.yaml"
SAFE_MCP_ENV_KEYS = frozenset(
    {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR"}
)
DEFAULT_MCP_TIMEOUT = 30.0
DEFAULT_MCP_CONNECT_TIMEOUT = 30.0


def load_mcp_servers(root: Path) -> dict[str, dict[str, Any]]:
    path = root / ".autonomy" / MCP_CONFIG_NAME
    if not path.is_file():
        return {}
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_servers = document.get("servers", document) if isinstance(document, dict) else None
    if not isinstance(raw_servers, dict):
        raise ValueError("mcp_servers.yaml must contain a mapping")
    return {
        str(name): dict(config)
        for name, config in raw_servers.items()
        if isinstance(config, dict)
    }


def sanitize_mcp_name_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))
    return sanitized.strip("_") or "unnamed"


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"mcp_{sanitize_mcp_name_component(server_name)}_{sanitize_mcp_name_component(tool_name)}"


def normalize_mcp_input_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    def rewrite(node: Any) -> Any:
        if isinstance(node, list):
            return [rewrite(item) for item in node]
        if not isinstance(node, dict):
            return node

        rewritten = {
            ("$defs" if key == "definitions" else key): rewrite(value)
            for key, value in node.items()
        }

        ref = rewritten.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            rewritten["$ref"] = "#/$defs/" + ref[len("#/definitions/"):]

        if rewritten.get("type") == "object" or "properties" in rewritten or "required" in rewritten:
            if not isinstance(rewritten.get("properties"), dict):
                rewritten["properties"] = {}
            required = rewritten.get("required")
            if isinstance(required, list):
                valid = [
                    item
                    for item in required
                    if isinstance(item, str) and item in rewritten["properties"]
                ]
                if valid:
                    rewritten["required"] = valid
                else:
                    rewritten.pop("required", None)
            elif "required" in rewritten:
                rewritten.pop("required", None)
            if "type" not in rewritten:
                rewritten["type"] = "object"

        return rewritten

    normalized = rewrite(schema)
    return normalized if isinstance(normalized, dict) else {"type": "object", "properties": {}}


def build_mcp_env(user_env: Mapping[str, str] | None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_MCP_ENV_KEYS
    }
    if user_env:
        env.update({str(key): str(value) for key, value in user_env.items()})
    return env


def sanitize_mcp_error(text: object) -> str:
    if text is None:
        return ""
    redacted, _ = redact_sensitive_text(str(text))
    return redacted.replace("***", "[REDACTED]")


def validate_mcp_http_url(server_name: str, url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"Invalid MCP URL for '{server_name}': expected non-empty string")
    stripped = url.strip()
    parsed = urlparse(stripped)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"Invalid MCP URL for '{server_name}': scheme must be http or https")
    if not parsed.hostname:
        raise ValueError(f"Invalid MCP URL for '{server_name}': missing host")
    return stripped


class McpServerSession:
    def __init__(self, name: str, config: dict[str, Any], client: Any):
        self.name = name
        self.config = config
        self.client = client

    def list_tools(self) -> list[Any]:
        return list(self.client.list_tools())

    def call_tool(self, tool_name: str, arguments: dict[str, Any], timeout: float) -> Any:
        return self.client.call_tool(tool_name, arguments, timeout=timeout)

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


def connect_mcp_server(name: str, config: dict[str, Any]) -> McpServerSession:
    client = _connect_with_mcp_sdk(name, config)
    return McpServerSession(name, config, client)


def _connect_with_mcp_sdk(name: str, config: dict[str, Any]) -> Any:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise ImportError("mcp package is not installed") from exc

    connect_timeout = float(config.get("connect_timeout", DEFAULT_MCP_CONNECT_TIMEOUT))
    stack = AsyncExitStack()
    ready: concurrent.futures.Future[Any] = concurrent.futures.Future()
    start_task_ready: concurrent.futures.Future[asyncio.Task] = concurrent.futures.Future()
    loop = asyncio.new_event_loop()

    async def start():
        try:
            if "url" in config:
                url = validate_mcp_http_url(name, config["url"])
                try:
                    from mcp.client.streamable_http import streamablehttp_client
                except ImportError as exc:
                    raise ImportError("mcp HTTP transport is not available") from exc
                streams = await stack.enter_async_context(
                    streamablehttp_client(url, headers=config.get("headers") or None)
                )
                read_stream, write_stream = streams[0], streams[1]
            else:
                command = str(config.get("command", "")).strip()
                if not command:
                    raise ValueError(f"MCP server '{name}' must define command or url")
                args = [str(item) for item in config.get("args", [])]
                params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=build_mcp_env(config.get("env")),
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            ready.set_result(session)
        except Exception as exc:
            try:
                await stack.aclose()
            finally:
                ready.set_exception(exc)

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        start_task_ready.set_result(loop.create_task(start()))
        loop.run_forever()

    thread = threading.Thread(target=run_loop, name=f"autonomy-mcp-connect-{name}", daemon=True)
    thread.start()

    start_task: asyncio.Task | None = None
    try:
        start_task = start_task_ready.result(5)
        session = ready.result(connect_timeout)
    except Exception:
        async def shutdown_startup() -> None:
            if start_task is not None and not start_task.done():
                start_task.cancel()
                try:
                    await start_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            await stack.aclose()

        try:
            asyncio.run_coroutine_threadsafe(shutdown_startup(), loop).result(5)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
        raise

    def close() -> None:
        async def shutdown() -> None:
            await stack.aclose()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(5)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)

    return McpSdkClient(name, session, close, loop=loop, thread=thread)


def _tool_attr(tool: Any, key: str, default: Any = None) -> Any:
    if isinstance(tool, dict):
        return tool.get(key, default)
    return getattr(tool, key, default)


def _mcp_tool_to_dict(tool: Any) -> dict[str, Any]:
    return {
        "name": _tool_attr(tool, "name", ""),
        "description": _tool_attr(tool, "description", ""),
        "inputSchema": _tool_attr(tool, "inputSchema", None),
    }


def _mcp_result_to_output(result: Any) -> Any:
    if _tool_attr(result, "isError", False):
        structured = _tool_attr(result, "structuredContent", None)
        if structured is not None:
            raise RuntimeError(_json_output(structured))
        content = _tool_attr(result, "content", None)
        if isinstance(content, list):
            message = []
            for block in content:
                text = _tool_attr(block, "text", None)
                message.append(str(text) if text is not None else str(block))
            raise RuntimeError("\n".join(message))
        raise RuntimeError(str(result))
    structured = _tool_attr(result, "structuredContent", None)
    if structured is not None:
        return structured
    content = _tool_attr(result, "content", None)
    if isinstance(content, list):
        text_parts = []
        raw_parts = []
        for block in content:
            text = _tool_attr(block, "text", None)
            if text is not None:
                text_parts.append(str(text))
            else:
                raw_parts.append(str(block))
        if text_parts and not raw_parts:
            return "\n".join(text_parts)
        return "\n".join([*text_parts, *raw_parts])
    return result


class McpSdkClient:
    def __init__(
        self,
        name: str,
        session: Any,
        close_callback,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        thread: threading.Thread | None = None,
    ):
        self.name = name
        self._session = session
        self._close_callback = close_callback
        self._owns_loop = loop is None
        self._loop = loop or asyncio.new_event_loop()
        self._thread = thread or threading.Thread(
            target=self._run_loop,
            name=f"autonomy-mcp-{name}",
            daemon=True,
        )
        if self._owns_loop:
            self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coroutine, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout)

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._run(self._session.list_tools(), DEFAULT_MCP_CONNECT_TIMEOUT)
        tools = _tool_attr(result, "tools", result)
        return [_mcp_tool_to_dict(tool) for tool in (tools or [])]

    def call_tool(self, tool_name: str, arguments: dict[str, Any], timeout: float) -> Any:
        result = self._run(self._session.call_tool(tool_name, arguments), timeout)
        return _mcp_result_to_output(result)

    def close(self) -> None:
        try:
            self._close_callback()
        finally:
            if self._owns_loop:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)


def _tool_allowed(tool_name: str, policy: object) -> bool:
    if not isinstance(policy, dict):
        return True
    include = policy.get("include")
    exclude = policy.get("exclude")
    if isinstance(include, str):
        include = [include]
    if isinstance(exclude, str):
        exclude = [exclude]
    if isinstance(include, list):
        return tool_name in {str(item) for item in include}
    if isinstance(exclude, list):
        return tool_name not in {str(item) for item in exclude}
    return True


def _argument_contract(schema: dict[str, Any]) -> dict[str, str]:
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return {}
    contract: dict[str, str] = {}
    for name, details in properties.items():
        if isinstance(details, dict):
            type_name = details.get("type", "value")
            description = details.get("description")
            contract[str(name)] = f"{type_name}: {description}" if description else str(type_name)
        else:
            contract[str(name)] = "value"
    return contract


def _json_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _make_mcp_handler(session: Any, tool_name: str, timeout: float):
    def handler(arguments: dict[str, Any]) -> Observation:
        try:
            result = session.call_tool(tool_name, dict(arguments), timeout)
        except Exception as exc:
            return Observation("", False, error=sanitize_mcp_error(exc), evidence=("mcp:error",))
        server_name = getattr(session, "name", "mcp")
        return Observation(
            "",
            True,
            output=_json_output(result),
            evidence=(f"mcp:{server_name}:{tool_name}",),
        )

    return handler


def register_mcp_tools(registry: ToolRegistry, root: Path) -> None:
    servers = load_mcp_servers(root)
    sessions: list[McpServerSession] = []
    try:
        for server_name, config in servers.items():
            if config.get("enabled", True) is False:
                continue
            try:
                session = connect_mcp_server(server_name, config)
            except Exception:
                continue
            sessions.append(session)
            registry.register_cleanup(session.close)
            timeout = float(config.get("timeout", DEFAULT_MCP_TIMEOUT))
            tools_policy = config.get("tools")
            for tool in session.list_tools():
                native_name = str(_tool_attr(tool, "name", "")).strip()
                if not native_name or not _tool_allowed(native_name, tools_policy):
                    continue
                schema = normalize_mcp_input_schema(_tool_attr(tool, "inputSchema", None))
                registry.register(
                    mcp_tool_name(server_name, native_name),
                    _make_mcp_handler(session, native_name, timeout),
                    description=str(
                        _tool_attr(tool, "description", "") or f"MCP tool {native_name} from {server_name}"
                    ),
                    toolset="mcp",
                    argument_contract=_argument_contract(schema),
                    default_risk=RiskLevel.MEDIUM,
                    side_effects=("external-mcp",),
                )
    except Exception:
        for session in reversed(sessions):
            try:
                session.close()
            except Exception:
                continue
        raise
