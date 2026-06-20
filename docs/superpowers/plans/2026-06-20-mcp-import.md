# MCP Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import external MCP server tools into Autonomy as normal `ToolRegistry` tools named `mcp_<server>_<tool>`.

**Architecture:** Add one focused `autonomy/tools/toolsets/mcp.py` module that owns config loading, MCP SDK adaptation, schema normalization, discovery, execution, and cleanup. Wire it into the existing toolset catalog and local registry so all MCP calls still pass through `ActionGateway` and `ToolRegistry`.

**Tech Stack:** Python 3.13, optional `mcp` package, PyYAML, existing `ToolRegistry`, existing `Observation`/`RiskLevel` models, mocked tests.

## Global Constraints

- Config path is `<workspace>/.autonomy/mcp_servers.yaml`.
- Registered MCP tools use `mcp_<server>_<tool>` names.
- MCP toolset is named `mcp`.
- MCP toolset is opt-in through `<workspace>/.autonomy/tools.yaml`.
- Do not implement Autonomy-as-MCP-server in this plan.
- Do not implement OAuth, SSE, sampling, dynamic `tools/list_changed`, hot reload, resources, or prompts in this plan.
- Tests use mocks only; no real subprocess or network MCP servers.
- Stdio subprocess environment includes only safe baseline keys plus explicit config `env`.
- HTTP URLs must be `http://` or `https://` and include a host.
- External MCP tool risk defaults to `MEDIUM`.
- Do not use `rm`, `rm -rf`, or `rmdir`; use `trash` for deletion.

---

## File Structure

- Create `autonomy/tools/toolsets/mcp.py`: MCP config, schema normalization, SDK adapter, server manager, registration, execution.
- Modify `autonomy/tools/local.py`: import and call `register_mcp_tools()` only when the `mcp` toolset should be discoverable.
- Modify `autonomy/toolsets.py`: add implemented `mcp` toolset and dynamic toolset status support.
- Modify `autonomy/cli.py`: build registry for `tools status` with loaded toolset config so disabled `mcp` does not start servers.
- Modify `pyproject.toml`: add optional `mcp` dependency extra.
- Modify `README.md`: document MCP config and enablement.
- Modify `tests/test_autonomy_native_tools.py`: add mocked unit and registry tests.
- Modify `tests/test_autonomy_native_cli.py`: add `tools status` dynamic MCP coverage.

---

### Task 1: MCP Config, Names, Schema, and Safety Helpers

**Files:**
- Create: `autonomy/tools/toolsets/mcp.py`
- Test: `tests/test_autonomy_native_tools.py`

**Interfaces:**
- Produces: `load_mcp_servers(root: Path) -> dict[str, dict[str, Any]]`
- Produces: `sanitize_mcp_name_component(value: str) -> str`
- Produces: `mcp_tool_name(server_name: str, tool_name: str) -> str`
- Produces: `normalize_mcp_input_schema(schema: dict[str, Any] | None) -> dict[str, Any]`
- Produces: `build_mcp_env(user_env: Mapping[str, str] | None) -> dict[str, str]`
- Produces: `sanitize_mcp_error(text: object) -> str`
- Produces: `validate_mcp_http_url(server_name: str, url: object) -> str`

- [ ] **Step 1: Write failing helper tests**

Append these tests near the database/toolset tests in `tests/test_autonomy_native_tools.py`:

```python
    def test_mcp_helpers_load_config_sanitize_names_and_normalize_schema(self):
        from autonomy.tools.toolsets.mcp import (
            load_mcp_servers,
            mcp_tool_name,
            normalize_mcp_input_schema,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".autonomy").mkdir()
            (root / ".autonomy" / "mcp_servers.yaml").write_text(
                "servers:\n"
                "  my-api:\n"
                "    command: npx\n"
                "    args: ['-y', '@example/server']\n"
                "    tools:\n"
                "      include: [query.data]\n",
                encoding="utf-8",
            )

            servers = load_mcp_servers(root)

        self.assertEqual(servers["my-api"]["command"], "npx")
        self.assertEqual(mcp_tool_name("my-api", "query.data"), "mcp_my_api_query_data")

        schema = normalize_mcp_input_schema(
            {
                "type": "object",
                "properties": {"payload": {"$ref": "#/definitions/Payload"}},
                "required": ["payload", "missing"],
                "definitions": {
                    "Payload": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q", "ghost"],
                    }
                },
            }
        )

        self.assertEqual(schema["required"], ["payload"])
        self.assertIn("$defs", schema)
        self.assertNotIn("definitions", schema)
        self.assertEqual(schema["properties"]["payload"]["$ref"], "#/$defs/Payload")
        self.assertEqual(schema["$defs"]["Payload"]["required"], ["q"])

    def test_mcp_helpers_filter_env_validate_url_and_redact_errors(self):
        from autonomy.tools.toolsets.mcp import (
            build_mcp_env,
            sanitize_mcp_error,
            validate_mcp_http_url,
        )

        with patch.dict(
            "os.environ",
            {
                "PATH": "/bin",
                "HOME": "/tmp/home",
                "OPENAI_API_KEY": "sk-secret",
                "XDG_CACHE_HOME": "/tmp/cache",
            },
            clear=True,
        ):
            env = build_mcp_env({"GITHUB_TOKEN": "ghp_custom"})

        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["HOME"], "/tmp/home")
        self.assertEqual(env["XDG_CACHE_HOME"], "/tmp/cache")
        self.assertEqual(env["GITHUB_TOKEN"], "ghp_custom")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(validate_mcp_http_url("remote", "https://example.test/mcp"), "https://example.test/mcp")
        with self.assertRaisesRegex(ValueError, "scheme must be http or https"):
            validate_mcp_http_url("remote", "file:///tmp/socket")
        self.assertNotIn("sk-secret", sanitize_mcp_error("failed with Bearer sk-secret"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_helpers_load_config_sanitize_names_and_normalize_schema tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_helpers_filter_env_validate_url_and_redact_errors -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autonomy.tools.toolsets.mcp'`.

- [ ] **Step 3: Add minimal helper implementation**

Create `autonomy/tools/toolsets/mcp.py`:

```python
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

SAFE_MCP_ENV_KEYS = frozenset({"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR"})
MCP_CONFIG_NAME = "mcp_servers.yaml"
DEFAULT_MCP_TIMEOUT = 120.0
DEFAULT_MCP_CONNECT_TIMEOUT = 30.0

_CREDENTIAL_PATTERN = re.compile(
    r"(?:ghp_[A-Za-z0-9_]{1,255}|sk-[A-Za-z0-9_]{1,255}|Bearer\s+\S+|"
    r"token=[^\s&,;\"']{1,255}|key=[^\s&,;\"']{1,255}|API_KEY=[^\s&,;\"']{1,255}|"
    r"password=[^\s&,;\"']{1,255}|secret=[^\s&,;\"']{1,255})",
    re.IGNORECASE,
)


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

    def rewrite_refs(node: Any) -> Any:
        if isinstance(node, dict):
            rewritten = {
                ("$defs" if key == "definitions" else key): rewrite_refs(value)
                for key, value in node.items()
            }
            ref = rewritten.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/definitions/"):
                rewritten["$ref"] = "#/$defs/" + ref[len("#/definitions/"):]
            return rewritten
        if isinstance(node, list):
            return [rewrite_refs(item) for item in node]
        return node

    def repair_objects(node: Any) -> Any:
        if isinstance(node, list):
            return [repair_objects(item) for item in node]
        if not isinstance(node, dict):
            return node
        repaired = {key: repair_objects(value) for key, value in node.items()}
        if not repaired.get("type") and ("properties" in repaired or "required" in repaired):
            repaired["type"] = "object"
        if repaired.get("type") == "object":
            if not isinstance(repaired.get("properties"), dict):
                repaired["properties"] = {}
            required = repaired.get("required")
            if isinstance(required, list):
                properties = repaired["properties"]
                valid = [item for item in required if isinstance(item, str) and item in properties]
                if valid:
                    repaired["required"] = valid
                else:
                    repaired.pop("required", None)
        return repaired

    normalized = repair_objects(rewrite_refs(schema))
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    if normalized.get("type") == "object" and not isinstance(normalized.get("properties"), dict):
        normalized["properties"] = {}
    return normalized


def build_mcp_env(user_env: Mapping[str, str] | None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_MCP_ENV_KEYS or key.startswith("XDG_")
    }
    if user_env:
        env.update({str(key): str(value) for key, value in user_env.items()})
    return env


def sanitize_mcp_error(text: object) -> str:
    return _CREDENTIAL_PATTERN.sub("[REDACTED]", str(text or ""))


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_helpers_load_config_sanitize_names_and_normalize_schema tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_helpers_filter_env_validate_url_and_redact_errors -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add autonomy/tools/toolsets/mcp.py tests/test_autonomy_native_tools.py
git commit -m "feat: add MCP import helpers"
```

---

### Task 2: MCP Discovery and Tool Registration

**Files:**
- Modify: `autonomy/tools/toolsets/mcp.py`
- Modify: `autonomy/tools/local.py`
- Modify: `autonomy/toolsets.py`
- Test: `tests/test_autonomy_native_tools.py`

**Interfaces:**
- Consumes: helpers from Task 1.
- Produces: `register_mcp_tools(registry: ToolRegistry, root: Path) -> None`
- Produces: `McpServerSession.close() -> None`
- Produces: `McpServerSession.call_tool(tool_name: str, arguments: dict[str, Any], timeout: float) -> Any`
- Produces: `connect_mcp_server(name: str, config: dict[str, Any]) -> McpServerSession`

- [ ] **Step 1: Write failing registration tests**

Append:

```python
    def test_mcp_toolset_registers_discovered_tools_with_filters(self):
        from autonomy.models import Observation
        from autonomy.tools.toolsets import mcp as mcp_toolset

        class FakeSession:
            def __init__(self):
                self.closed = False

            def list_tools(self):
                return [
                    {
                        "name": "read-file",
                        "description": "Read a file",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                    {
                        "name": "write_file",
                        "description": "Write a file",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]

            def call_tool(self, tool_name, arguments, timeout):
                return {"tool": tool_name, "arguments": arguments, "timeout": timeout}

            def close(self):
                self.closed = True

        fake_session = FakeSession()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".autonomy").mkdir()
            (root / ".autonomy" / "mcp_servers.yaml").write_text(
                "servers:\n"
                "  fs-server:\n"
                "    command: fake-mcp\n"
                "    timeout: 7\n"
                "    tools:\n"
                "      include: [read-file]\n",
                encoding="utf-8",
            )

            with patch.object(mcp_toolset, "connect_mcp_server", return_value=fake_session):
                registry = build_local_tool_registry(
                    root,
                    ToolsetConfiguration(enabled_toolsets=("mcp",)),
                )

            self.assertEqual(registry.names, {"mcp_fs_server_read_file"})
            spec = registry.spec("mcp_fs_server_read_file")
            self.assertEqual(spec.toolset, "mcp")
            self.assertEqual(spec.default_risk, RiskLevel.MEDIUM)
            self.assertEqual(spec.argument_contract["path"], "string")
            result = registry.execute(
                Action(
                    "mcp_fs_server_read_file",
                    {"path": "README.md"},
                    "read via mcp",
                    "verify",
                )
            )
            registry.close()

        self.assertTrue(result.succeeded, result.error)
        self.assertIn('"tool": "read-file"', result.output)
        self.assertTrue(fake_session.closed)

    def test_mcp_toolset_is_not_registered_when_disabled(self):
        from autonomy.tools.toolsets import mcp as mcp_toolset

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".autonomy").mkdir()
            (root / ".autonomy" / "mcp_servers.yaml").write_text(
                "servers:\n"
                "  fs:\n"
                "    command: fake-mcp\n",
                encoding="utf-8",
            )

            with patch.object(mcp_toolset, "connect_mcp_server") as connect:
                registry = build_local_tool_registry(root, ToolsetConfiguration())

        self.assertNotIn("mcp", {registry.spec(name).toolset for name in registry.names})
        self.assertEqual(connect.call_count, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_toolset_registers_discovered_tools_with_filters tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_toolset_is_not_registered_when_disabled -q
```

Expected: FAIL because `register_mcp_tools` and `mcp` catalog wiring do not exist.

- [ ] **Step 3: Implement minimal registration**

Extend `autonomy/tools/toolsets/mcp.py` with:

```python
import json

from ...models import Observation, RiskLevel
from ..registry import ToolRegistry


class McpServerSession:
    def __init__(self, name: str, config: dict[str, Any], client: Any):
        self.name = name
        self.config = config
        self.client = client

    def list_tools(self) -> list[Any]:
        return self.client.list_tools()

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
    raise ImportError("mcp package is not installed")


def _tool_attr(tool: Any, key: str, default: Any = None) -> Any:
    if isinstance(tool, dict):
        return tool.get(key, default)
    return getattr(tool, key, default)


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
    contract = {}
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


def _make_mcp_handler(session: McpServerSession, tool_name: str, timeout: float):
    def handler(arguments: dict[str, Any]) -> Observation:
        try:
            result = session.call_tool(tool_name, dict(arguments), timeout)
        except Exception as exc:
            return Observation("", False, error=sanitize_mcp_error(exc), evidence=("mcp:error",))
        return Observation(
            "",
            True,
            output=_json_output(result),
            evidence=(f"mcp:{session.name}:{tool_name}",),
        )

    return handler


def register_mcp_tools(registry: ToolRegistry, root: Path) -> None:
    servers = load_mcp_servers(root)
    sessions: list[McpServerSession] = []
    for server_name, config in servers.items():
        if config.get("enabled", True) is False:
            continue
        session = connect_mcp_server(server_name, config)
        sessions.append(session)
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
                description=str(_tool_attr(tool, "description", "") or f"MCP tool {native_name} from {server_name}"),
                toolset="mcp",
                argument_contract=_argument_contract(schema),
                default_risk=RiskLevel.MEDIUM,
                side_effects=("external-mcp",),
            )
    for session in sessions:
        registry.register_cleanup(session.close)
```

Modify `autonomy/toolsets.py` catalog:

```python
    ToolsetDefinition(
        "mcp",
        "External Model Context Protocol tools imported from configured MCP servers.",
        "implemented",
    ),
```

Modify `autonomy/tools/local.py` imports:

```python
from .toolsets.mcp import register_mcp_tools
```

Modify `build_local_tool_registry()` near other toolset registrations:

```python
    if toolsets is None or "mcp" in toolsets.enabled_set:
        register_mcp_tools(registry, root)
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_toolset_registers_discovered_tools_with_filters tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_toolset_is_not_registered_when_disabled -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add autonomy/tools/toolsets/mcp.py autonomy/tools/local.py autonomy/toolsets.py tests/test_autonomy_native_tools.py
git commit -m "feat: register MCP tools"
```

---

### Task 3: MCP SDK Adapter and Error Paths

**Files:**
- Modify: `autonomy/tools/toolsets/mcp.py`
- Test: `tests/test_autonomy_native_tools.py`

**Interfaces:**
- Consumes: `connect_mcp_server()` from Task 2.
- Produces: `_connect_with_mcp_sdk(name: str, config: dict[str, Any]) -> McpSdkClient`
- Produces: discovery failure behavior that does not crash registry creation.

- [ ] **Step 1: Write failing missing-SDK, client adapter, and failure tests**

Append:

```python
    def test_mcp_missing_sdk_does_not_crash_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".autonomy").mkdir()
            (root / ".autonomy" / "mcp_servers.yaml").write_text(
                "servers:\n"
                "  fs:\n"
                "    command: fake-mcp\n",
                encoding="utf-8",
            )

            registry = build_local_tool_registry(
                root,
                ToolsetConfiguration(enabled_toolsets=("mcp",)),
            )

        self.assertEqual(registry.names, set())

    def test_mcp_tool_handler_redacts_errors(self):
        from autonomy.tools.toolsets.mcp import McpServerSession, _make_mcp_handler

        class FailingClient:
            def call_tool(self, tool_name, arguments, timeout):
                raise RuntimeError("bad token=sk-secret")

        session = McpServerSession("github", {}, FailingClient())
        observation = _make_mcp_handler(session, "create_issue", 1)({})

        self.assertFalse(observation.succeeded)
        self.assertNotIn("sk-secret", observation.error)
        self.assertIn("[REDACTED]", observation.error)

    def test_mcp_sdk_client_wraps_async_session(self):
        from autonomy.tools.toolsets.mcp import McpSdkClient

        class FakeToolResult:
            content = [type("Block", (), {"text": "hello"})()]
            isError = False

        class FakeToolsResult:
            tools = [{"name": "echo", "description": "Echo", "inputSchema": {"type": "object", "properties": {}}}]

        class FakeSession:
            async def list_tools(self):
                return FakeToolsResult()

            async def call_tool(self, tool_name, arguments):
                self.last_call = (tool_name, arguments)
                return FakeToolResult()

        client = McpSdkClient("fake", FakeSession(), close_callback=lambda: None)
        try:
            self.assertEqual(client.list_tools()[0]["name"], "echo")
            self.assertEqual(client.call_tool("echo", {"text": "hi"}, timeout=1), "hello")
        finally:
            client.close()
```

- [ ] **Step 2: Run tests to verify current behavior**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_missing_sdk_does_not_crash_registry tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_tool_handler_redacts_errors tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_sdk_client_wraps_async_session -q
```

Expected: FAIL because `McpSdkClient` does not exist and `register_mcp_tools()` propagates SDK failures.

- [ ] **Step 3: Catch discovery failures and add real SDK adapter**

Change `register_mcp_tools()` server connect block:

```python
        try:
            session = connect_mcp_server(server_name, config)
        except Exception:
            continue
```

Add imports near the top of `mcp.py`:

```python
import asyncio
import concurrent.futures
import threading
from contextlib import AsyncExitStack
```

Add SDK result conversion and client wrapper:

```python
def _mcp_tool_to_dict(tool: Any) -> dict[str, Any]:
    return {
        "name": _tool_attr(tool, "name", ""),
        "description": _tool_attr(tool, "description", ""),
        "inputSchema": _tool_attr(tool, "inputSchema", None),
    }


def _mcp_result_to_output(result: Any) -> Any:
    if getattr(result, "isError", False):
        raise RuntimeError(_mcp_result_to_output({**getattr(result, "__dict__", {}), "isError": True}))
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(result, "content", None)
    if isinstance(content, list):
        text_parts = []
        raw_parts = []
        for block in content:
            text = getattr(block, "text", None)
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
        self._thread = thread or threading.Thread(target=self._run_loop, name=f"autonomy-mcp-{name}", daemon=True)
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
        tools = getattr(result, "tools", result)
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
```

Replace `_connect_with_mcp_sdk()` with this real adapter:

```python
def _connect_with_mcp_sdk(name: str, config: dict[str, Any]) -> Any:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise ImportError("mcp package is not installed") from exc

    connect_timeout = float(config.get("connect_timeout", DEFAULT_MCP_CONNECT_TIMEOUT))
    stack = AsyncExitStack()
    ready: concurrent.futures.Future[Any] = concurrent.futures.Future()
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
            ready.set_exception(exc)

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start())
        loop.run_forever()

    thread = threading.Thread(target=run_loop, name=f"autonomy-mcp-connect-{name}", daemon=True)
    thread.start()
    session = ready.result(connect_timeout)

    def close() -> None:
        async def shutdown() -> None:
            await stack.aclose()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(5)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)

    return McpSdkClient(name, session, close, loop=loop, thread=thread)
```

This adapter keeps one MCP session alive per server and still lets tests replace `connect_mcp_server()` with a fake session.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_missing_sdk_does_not_crash_registry tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_tool_handler_redacts_errors tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_sdk_client_wraps_async_session -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add autonomy/tools/toolsets/mcp.py tests/test_autonomy_native_tools.py
git commit -m "feat: connect MCP SDK sessions"
```

---

### Task 4: Status, Docs, and Final Verification

**Files:**
- Modify: `autonomy/toolsets.py`
- Modify: `autonomy/cli.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Test: `tests/test_autonomy_native_tools.py`
- Test: `tests/test_autonomy_native_cli.py`

**Interfaces:**
- Consumes: registered `mcp` toolset from Task 2.
- Produces: `toolset_catalog_status()` dynamic `mcp` tools display.
- Produces: CLI `tools status` that builds registry with loaded toolset configuration.

- [ ] **Step 1: Write failing status tests**

Append to `tests/test_autonomy_native_tools.py`:

```python
    def test_mcp_toolset_catalog_status_includes_dynamic_tools(self):
        from autonomy.toolsets import toolset_catalog_status

        rows = toolset_catalog_status(
            ToolsetConfiguration(enabled_toolsets=("mcp",)),
            {
                "mcp_fs_read_file": {
                    "toolset": "mcp",
                    "available": True,
                    "unavailable_reason": "",
                }
            },
        )

        mcp_row = next(row for row in rows if row["name"] == "mcp")
        self.assertTrue(mcp_row["implemented"])
        self.assertEqual(mcp_row["tools"], ["mcp_fs_read_file"])
        self.assertEqual(mcp_row["available_tools"], ["mcp_fs_read_file"])
```

Append this method to `AutonomyNativeCliTest` in `tests/test_autonomy_native_cli.py`:

```python
    def test_tools_status_uses_loaded_toolset_config_for_mcp_discovery(self):
        from autonomy import ToolsetConfiguration
        from autonomy.tools.toolsets import mcp as mcp_toolset

        class FakeSession:
            def list_tools(self):
                return [
                    {
                        "name": "read_file",
                        "description": "Read",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]

            def call_tool(self, tool_name, arguments, timeout):
                return "ok"

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".autonomy").mkdir()
            ToolsetConfigStore(root / ".autonomy").save(
                ToolsetConfiguration(enabled_toolsets=("mcp",))
            )
            (root / ".autonomy" / "mcp_servers.yaml").write_text(
                "servers:\n"
                "  fs:\n"
                "    command: fake-mcp\n",
                encoding="utf-8",
            )
            with (
                patch("autonomy.cli._workspace_for_args", return_value=root),
                patch.object(mcp_toolset, "connect_mcp_server", return_value=FakeSession()),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = main(["tools", "status"])

        self.assertEqual(result, 0)
        self.assertIn("mcp_fs_read_file", output.getvalue())
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_toolset_catalog_status_includes_dynamic_tools -q
```

Expected: FAIL because the `mcp` catalog row has no dynamic tools.

Run the CLI test:

```bash
python3.13 -m pytest tests/test_autonomy_native_cli.py::AutonomyNativeCliTest::test_tools_status_uses_loaded_toolset_config_for_mcp_discovery -q
```

Expected: FAIL until CLI status uses loaded config.

- [ ] **Step 3: Implement dynamic status and CLI status config**

Modify `toolset_catalog_status()` in `autonomy/toolsets.py` before `visible_tools`:

```python
        dynamic_tools = tuple(
            sorted(
                name
                for name, status in tool_statuses.items()
                if status.get("toolset") == definition.name and name not in definition.tools
            )
        )
        visible_tools = tuple(
            tool
            for tool in (*definition.tools, *dynamic_tools)
            if tool not in disabled_tools
        )
```

Modify both CLI tools status paths in `autonomy/cli.py`:

```python
                configuration = store.load()
                registry = build_local_tool_registry(self.workspace, configuration)
                self._write(
                    json.dumps(
                        toolset_catalog_status(
                            configuration,
                            registry.tool_statuses(),
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
```

and:

```python
                configuration = toolset_store.load()
                registry = build_local_tool_registry(workspace, configuration)
                print(
                    json.dumps(
                        toolset_catalog_status(
                            configuration,
                            registry.tool_statuses(),
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
```

- [ ] **Step 4: Add optional dependency and docs**

Modify `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = ["pytest>=9.0"]
mcp = ["mcp>=1.26.0"]
```

Add to `README.md` after the Toolsets section:

```markdown
### MCP Tool Import

Autonomy can import external MCP server tools through the optional `mcp`
toolset.

Install MCP support:

```bash
python -m pip install -e ".[mcp]"
```

Configure servers in:

```yaml
# <workspace>/.autonomy/mcp_servers.yaml
servers:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    tools:
      include: [read_file]
```

Enable the toolset:

```bash
autonomy tools enable mcp
autonomy tools status
```

Discovered tools appear as `mcp_<server>_<tool>` and still execute through
`ActionGateway`.
```

- [ ] **Step 5: Run targeted tests**

Run:

```bash
python3.13 -m pytest tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_helpers_load_config_sanitize_names_and_normalize_schema tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_helpers_filter_env_validate_url_and_redact_errors tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_toolset_registers_discovered_tools_with_filters tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_toolset_is_not_registered_when_disabled tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_missing_sdk_does_not_crash_registry tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_tool_handler_redacts_errors tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_sdk_client_wraps_async_session tests/test_autonomy_native_tools.py::AutonomyNativeToolsTest::test_mcp_toolset_catalog_status_includes_dynamic_tools -q
```

Expected: `8 passed`.

- [ ] **Step 6: Run full verification**

Run:

```bash
python3.13 -m pytest -q
python3.13 -m autonomy tools status
git diff --check
```

Expected:

- pytest: all tests pass.
- tools status: `mcp` row exists, implemented true, disabled unless enabled in workspace config.
- diff check: no output.

- [ ] **Step 7: Commit**

```bash
git add autonomy/toolsets.py autonomy/cli.py pyproject.toml README.md tests/test_autonomy_native_tools.py tests/test_autonomy_native_cli.py
git commit -m "feat: expose MCP toolset status"
```

---

## Self-Review

- Spec coverage: config, naming, opt-in toolset, schema normalization, safe env, URL validation, redaction, mocked tests, status, docs, and optional dependency are covered.
- Scope check: Autonomy-as-MCP-server, OAuth, SSE, sampling, resources/prompts, hot reload, and dynamic notifications are excluded from all tasks.
- Type consistency: `register_mcp_tools(registry, root)`, `McpServerSession.call_tool(tool_name, arguments, timeout)`, and `mcp_tool_name(server_name, tool_name)` are consistently named across tasks.
- Execution risk: Task 3 implements the real stdio/HTTP SDK adapter, but tests still mock discovery and execution so verification does not spawn subprocesses or use network.
