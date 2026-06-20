# MCP Import Design

## Goal

Add external MCP tool import to Autonomy without bypassing Autonomy's existing execution model.

Configured MCP servers are discovered from `<workspace>/.autonomy/mcp_servers.yaml`. Each discovered MCP tool is registered as a normal `ToolRegistry` tool named `mcp_<server>_<tool>`, assigned to the `mcp` toolset, and executed through `ActionGateway` like any native tool.

## Non-Goals

- Autonomy-as-MCP-server.
- OAuth, SSE, sampling, dynamic `tools/list_changed`, and hot reload.
- MCP resources/prompts utility wrappers.
- Real subprocess or network servers in tests.

These can be added later when a concrete server requires them.

## User-Facing Config

```yaml
# <workspace>/.autonomy/mcp_servers.yaml
servers:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    env: {}
    timeout: 120
    connect_timeout: 30
    tools:
      include: [read_file]
```

Supported server keys:

- `command`: stdio executable.
- `args`: stdio arguments.
- `env`: explicit environment for the subprocess.
- `url`: remote HTTP endpoint, if the installed MCP SDK supports it.
- `headers`: HTTP headers for remote servers.
- `enabled`: skip server when `false`.
- `timeout`: per-tool timeout.
- `connect_timeout`: discovery timeout.
- `tools.include`: allowlist native MCP tool names.
- `tools.exclude`: denylist native MCP tool names.

The `mcp` toolset remains opt-in through `<workspace>/.autonomy/tools.yaml`.

## Architecture

Add `autonomy/tools/toolsets/mcp.py`.

Responsibilities:

- Read and validate `.autonomy/mcp_servers.yaml`.
- Start MCP sessions for enabled servers during `build_local_tool_registry()`.
- Discover MCP tools.
- Register each tool into `ToolRegistry` as `mcp_<server>_<tool>`.
- Keep MCP sessions alive for the lifetime of the registry.
- Register cleanup callbacks so `ToolRegistry.close()` closes sessions.

Update:

- `autonomy/toolsets.py`: add implemented `mcp` toolset.
- `autonomy/tools/local.py`: call `register_mcp_tools(registry, root)`.
- `toolset_catalog_status()`: include dynamic registry tools whose `toolset`
  is `mcp`, because MCP tool names are not known at import time.
- `README.md`: document config, install extra, and enablement.

## Tool Names

Server and tool name components are sanitized by replacing non `[A-Za-z0-9_]` characters with `_`.

Examples:

- server `filesystem`, tool `read_file` -> `mcp_filesystem_read_file`
- server `my-api`, tool `query.data` -> `mcp_my_api_query_data`

Name collisions fail during registration with the existing `ToolRegistry.register()` duplicate check.

## Schema Handling

MCP `inputSchema` becomes the Autonomy `argument_contract`.

Minimum normalization:

- Missing schema -> `{"type": "object", "properties": {}}`.
- Object schema without `properties` gets `{}`.
- Dangling `required` names are pruned.
- `definitions` and `#/definitions/...` refs become `$defs` and `#/$defs/...`.

No provider-specific schema output is added. Autonomy continues using its current model spec path.

## Safety

Stdio subprocess environment:

- Pass only safe baseline environment keys (`PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `TERM`, `SHELL`, `TMPDIR`) plus explicit config `env`.
- Do not inherit arbitrary secrets.

HTTP:

- Accept only `http://` and `https://` URLs with a host.

Errors:

- Redact common credential patterns before returning errors to the model.
- Failed MCP SDK import or failed server discovery marks tools unavailable through `tools status`; it does not crash registry construction.

Execution:

- MCP tool calls use the configured timeout.
- MCP tool results are converted to JSON when structured, or text otherwise.
- Tool risk defaults to `MEDIUM` because external tools can have unknown side effects.

## Data Flow

1. CLI/session loads `ToolsetConfiguration`.
2. `build_local_tool_registry(workspace, toolsets)` builds full registry.
3. MCP registration reads `.autonomy/mcp_servers.yaml`.
4. Enabled servers connect and list tools.
5. Matching tools pass include/exclude filtering.
6. Each tool is registered under toolset `mcp`.
7. `filter_by_toolsets()` exposes tools only when `mcp` is enabled.
8. Model calls `mcp_<server>_<tool>` through `ActionGateway`.
9. Handler calls the MCP server and returns an `Observation`.

## Error Handling

- Missing config: no tools registered.
- Missing `mcp` package: `mcp` toolset is implemented but unavailable with a compact reason.
- Bad server config: server is skipped with an unavailable reason.
- Discovery timeout: server is skipped with an unavailable reason.
- Tool call timeout: action returns failed `Observation`.
- Server error: action returns failed `Observation` with redacted message.
- `tools status` lists discovered `mcp_<server>_<tool>` names when discovery
  succeeds and reports compact unavailable reasons when discovery fails.

## Testing

Use mocks only.

Required tests:

- Config loader accepts `servers:` and legacy top-level mappings.
- Name sanitizer produces provider-safe tool names.
- Schema normalization handles empty schema, missing properties, dangling `required`, and `definitions`.
- Registration creates `mcp_<server>_<tool>` under toolset `mcp`.
- Include/exclude filters work, with include taking precedence.
- Missing MCP SDK does not crash and surfaces unavailable status.
- Tool handler returns successful `Observation` for text content.
- Tool handler redacts secrets from errors.
- `python3.13 -m autonomy tools status` shows `mcp` implemented and includes
  discovered dynamic MCP tool names.

Targeted verification:

```bash
python3.13 -m pytest tests/test_autonomy_native_tools.py -q
python3.13 -m pytest -q
python3.13 -m autonomy tools status
git diff --check
```

## Open Decisions

None for v1.

Future work is intentionally deferred until a real configured server needs it.
