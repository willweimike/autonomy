from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml


DEFAULT_ENABLED_TOOLSETS = ("assistant", "browser", "file", "terminal", "search", "skills", "memory")


@dataclass(frozen=True)
class ToolsetDefinition:
    name: str
    description: str
    status: str = "planned"
    tools: tuple[str, ...] = ()


TOOLSET_CATALOG: tuple[ToolsetDefinition, ...] = (
    ToolsetDefinition(
        "assistant",
        "Direct assistant response tools.",
        "implemented",
        ("assistant.respond",),
    ),
    ToolsetDefinition("search", "Search tools.", "implemented", ("search.text",)),
    ToolsetDefinition("vision", "Image and visual understanding tools."),
    ToolsetDefinition("image_gen", "Image generation tools."),
    ToolsetDefinition(
        "terminal",
        "Terminal command execution and process management tools.",
        "implemented",
        (
            "shell.execute",
            "process.start",
            "process.poll",
            "process.log",
            "process.wait",
            "process.stop",
        ),
    ),
    ToolsetDefinition(
        "file",
        "Workspace file read, list, and manipulation tools.",
        "implemented",
        (
            "filesystem.read",
            "filesystem.read_many",
            "filesystem.list",
            "filesystem.tree",
            "filesystem.stat",
            "filesystem.stat_many",
            "filesystem.diff",
            "filesystem.write",
            "filesystem.patch",
            "filesystem.trash",
            "filesystem.mkdir",
            "filesystem.move",
            "filesystem.search_files",
            "filesystem.outline",
            "filesystem.imports",
            "filesystem.symbol_search",
            "filesystem.syntax_check",
        ),
    ),
    ToolsetDefinition(
        "browser",
        "Browser automation tools.",
        "implemented",
        (
            "browser.navigate",
            "browser.snapshot",
            "browser.click",
            "browser.type",
            "browser.scroll",
            "browser.back",
            "browser.press",
            "browser.screenshot",
            "browser.get_images",
            "browser.console",
            "browser.dialog",
        ),
    ),
    ToolsetDefinition(
        "project",
        "Read-only project and version-control inspection tools.",
        "implemented",
        (
            "git.status",
            "git.diff",
            "git.log",
            "git.show",
            "json.parse",
            "yaml.parse",
            "project.detect",
            "python.test_discover",
        ),
    ),
    ToolsetDefinition("skills", "Procedure skill management and discovery tools.", "implemented"),
    ToolsetDefinition("todo", "Task planning and tracking tools."),
    ToolsetDefinition(
        "memory",
        "Persistent memory tools.",
        "implemented",
        (
            "memory.remember",
            "memory.recall",
            "memory.list",
            "memory.forget",
        ),
    ),
    ToolsetDefinition(
        "database",
        "SQLGlot-backed read-only database retrieval and SQL generation tools.",
        "implemented",
        ("database.retrieve",),
    ),
    ToolsetDefinition(
        "mcp",
        "External Model Context Protocol tools imported from configured MCP servers.",
        "implemented",
    ),
    ToolsetDefinition("session_search", "Conversation history search tools."),
    ToolsetDefinition("clarify", "Clarifying question tools."),
    ToolsetDefinition("code_execution", "Programmatic code execution tools."),
    ToolsetDefinition(
        "delegate",
        "Child task delegation tools.",
        "implemented",
        ("delegate.run",),
    ),
    ToolsetDefinition("cronjob", "Scheduled task tools."),
    ToolsetDefinition("messaging", "Cross-platform messaging tools."),
    ToolsetDefinition("computer_use", "Desktop computer-use tools."),
)


CATALOG_BY_NAME = {definition.name: definition for definition in TOOLSET_CATALOG}
_MAX_UNAVAILABLE_REASON_CHARS = 500


class ToolsetConfigurationError(ValueError):
    """Invalid toolset configuration."""


@dataclass(frozen=True)
class ToolsetConfiguration:
    enabled_toolsets: tuple[str, ...] = DEFAULT_ENABLED_TOOLSETS
    disabled_tools: tuple[str, ...] = ()

    def validate(self) -> None:
        unknown_toolsets = sorted(set(self.enabled_toolsets) - set(CATALOG_BY_NAME))
        if unknown_toolsets:
            raise ToolsetConfigurationError(
                "unknown toolsets: " + ", ".join(unknown_toolsets)
            )
        if not all(isinstance(item, str) and item.strip() for item in self.disabled_tools):
            raise ToolsetConfigurationError("disabled_tools must contain non-empty strings")

    @property
    def enabled_set(self) -> set[str]:
        return set(self.enabled_toolsets)

    @property
    def disabled_tool_set(self) -> set[str]:
        return set(self.disabled_tools)

    def as_document(self) -> dict:
        return {
            "version": 1,
            "tools": {
                "enabled_toolsets": list(self.enabled_toolsets),
                "disabled_tools": list(self.disabled_tools),
            },
        }


class ToolsetConfigStore:
    def __init__(self, config_dir: Path | None = None):
        self.config_dir = (config_dir or Path.cwd() / ".autonomy").expanduser()
        self.config_path = self.config_dir / "tools.yaml"

    def load(self) -> ToolsetConfiguration:
        if not self.config_path.is_file():
            return ToolsetConfiguration()
        try:
            document = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ToolsetConfigurationError(f"could not read tool configuration: {exc}") from exc
        try:
            if not isinstance(document, dict) or document.get("version") != 1:
                raise TypeError("version must be 1")
            payload = document["tools"]
            if not isinstance(payload, dict):
                raise TypeError("tools must be an object")
            configuration = ToolsetConfiguration(
                enabled_toolsets=self._string_tuple(
                    payload.get("enabled_toolsets", list(DEFAULT_ENABLED_TOOLSETS)),
                    "enabled_toolsets",
                ),
                disabled_tools=self._string_tuple(
                    payload.get("disabled_tools", []),
                    "disabled_tools",
                ),
            )
            configuration.validate()
            return configuration
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolsetConfigurationError(f"tool configuration is invalid: {exc}") from exc

    def save(self, configuration: ToolsetConfiguration) -> None:
        configuration.validate()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        document = yaml.safe_dump(
            configuration.as_document(),
            sort_keys=False,
            allow_unicode=False,
        )
        self._atomic_write(self.config_path, document)

    def enable(self, toolset: str) -> ToolsetConfiguration:
        if toolset not in CATALOG_BY_NAME:
            raise ToolsetConfigurationError(f"unknown toolset: {toolset}")
        current = self.load()
        enabled = tuple(sorted(current.enabled_set | {toolset}))
        updated = ToolsetConfiguration(enabled, current.disabled_tools)
        self.save(updated)
        return updated

    def disable(self, toolset: str) -> ToolsetConfiguration:
        if toolset not in CATALOG_BY_NAME:
            raise ToolsetConfigurationError(f"unknown toolset: {toolset}")
        current = self.load()
        enabled = tuple(sorted(current.enabled_set - {toolset}))
        updated = ToolsetConfiguration(enabled, current.disabled_tools)
        self.save(updated)
        return updated

    @staticmethod
    def _string_tuple(value, name: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"{name} must be an array of strings")
        return tuple(item.strip() for item in value if item.strip())

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()


def toolset_catalog_status(
    configuration: ToolsetConfiguration,
    tool_statuses: dict[str, dict] | None = None,
) -> list[dict]:
    enabled = configuration.enabled_set
    disabled_tools = configuration.disabled_tool_set
    tool_statuses = tool_statuses or {}
    rows: list[dict] = []
    for definition in TOOLSET_CATALOG:
        implemented = definition.status == "implemented"
        dynamic_tools = tuple(
            sorted(
                tool
                for tool, status in tool_statuses.items()
                if status.get("toolset") == definition.name and tool not in definition.tools
            )
        )
        catalog_tools = (*definition.tools, *dynamic_tools)
        visible_tools = tuple(tool for tool in catalog_tools if tool not in disabled_tools)
        available_tools = tuple(
            tool
            for tool in visible_tools
            if tool_statuses.get(tool, {}).get("available", True)
        )
        unavailable_tools = [
            {
                "tool": tool,
                "reason": _compact_unavailable_reason(
                    tool_statuses.get(tool, {}).get("unavailable_reason", "")
                ),
            }
            for tool in visible_tools
            if not tool_statuses.get(tool, {}).get("available", True)
        ]
        rows.append(
            {
                "name": definition.name,
                "description": definition.description,
                "status": definition.status,
                "implemented": implemented,
                "enabled": definition.name in enabled,
                "tools": list(catalog_tools),
                "available_tools": list(available_tools) if implemented and definition.name in enabled else [],
                "unavailable_tools": unavailable_tools if implemented else [],
            }
        )
    return rows


def _compact_unavailable_reason(reason: object) -> str:
    text = str(reason or "").strip()
    if not text:
        return ""
    if "Browser logs:" in text:
        text = text.split("Browser logs:", 1)[0].strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = " ".join(lines) if lines else text
    if len(compact) > _MAX_UNAVAILABLE_REASON_CHARS:
        compact = compact[: _MAX_UNAVAILABLE_REASON_CHARS - 3].rstrip() + "..."
    return compact
