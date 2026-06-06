from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .models import Action, ActionIntent, Observation, RiskLevel


ToolHandler = Callable[[dict], Observation]
ToolValidator = Callable[[dict], None]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    toolset: str
    argument_contract: dict[str, str]
    default_risk: RiskLevel
    side_effects: tuple[str, ...] = ()
    handler: ToolHandler = field(repr=False, compare=False, default=lambda arguments: Observation("", False))
    validator: ToolValidator | None = field(repr=False, compare=False, default=None)

    @property
    def summary(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "toolset": self.toolset,
            "argument_contract": self.argument_contract,
            "default_risk": self.default_risk.value,
            "side_effects": self.side_effects,
        }


class ToolRegistry:
    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}

    @property
    def names(self) -> set[str]:
        return set(self._specs)

    @property
    def contracts(self) -> dict[str, dict[str, str]]:
        return {
            name: dict(spec.argument_contract)
            for name, spec in self._specs.items()
        }

    def register(
        self,
        name: str,
        handler: ToolHandler,
        validator: ToolValidator | None = None,
        *,
        description: str = "",
        toolset: str = "general",
        argument_contract: dict[str, str] | None = None,
        default_risk: RiskLevel = RiskLevel.LOW,
        side_effects: tuple[str, ...] = (),
    ) -> None:
        if name in self._specs:
            raise ValueError(f"tool already registered: {name}")
        self._specs[name] = ToolSpec(
            name=name,
            description=description or name,
            toolset=toolset,
            argument_contract=argument_contract or {},
            default_risk=default_risk,
            side_effects=side_effects,
            handler=handler,
            validator=validator,
        )

    def spec(self, tool_name: str) -> ToolSpec:
        if tool_name not in self._specs:
            raise KeyError(f"unknown tool: {tool_name}")
        return self._specs[tool_name]

    def rejection_reason(self, intent: ActionIntent | Action) -> str:
        spec = self._specs.get(intent.tool)
        if not spec or not spec.validator:
            return ""
        try:
            spec.validator(intent.arguments)
        except (KeyError, TypeError, ValueError) as exc:
            return f"invalid arguments for {intent.tool}: {exc}"
        return ""

    def action_from_intent(self, intent: ActionIntent) -> Action:
        spec = self.spec(intent.tool)
        purpose = intent.purpose.strip()
        expected_effect = purpose or spec.description
        verification_plan = (
            f"Verify the {intent.tool} observation against the goal, "
            "the action purpose, deterministic evidence, and tool result."
        )
        return Action(
            tool=intent.tool,
            arguments=dict(intent.arguments),
            expected_effect=expected_effect,
            verification_plan=verification_plan,
            purpose=purpose,
            risk_level=spec.default_risk,
            edge_confidence=intent.edge_confidence,
            evidence_strength=intent.evidence_strength,
            recipe_id=intent.recipe_id,
            edge_ids=intent.edge_ids,
        )

    def execute(self, action: Action) -> Observation:
        if action.tool not in self._specs:
            raise KeyError(f"unknown tool: {action.tool}")
        try:
            observation = self._specs[action.tool].handler(action.arguments)
        except Exception as exc:
            return Observation(
                action_id=action.id,
                succeeded=False,
                error=f"{type(exc).__name__}: {exc}",
                evidence=(f"tool_error:{type(exc).__name__}",),
            )
        return Observation(
            action_id=action.id,
            succeeded=observation.succeeded,
            output=observation.output,
            error=observation.error,
            evidence=observation.evidence,
            side_effects=observation.side_effects,
            exit_code=observation.exit_code,
        )


class ApprovalPolicy:
    SAFE_SHELL_PREFIXES = (
        "cat ",
        "find ",
        "git diff",
        "git log",
        "git show",
        "git status",
        "head ",
        "ls",
        "pwd",
        "python3.13 -m pytest",
        "rg ",
        "sed ",
        "tail ",
        "wc ",
    )

    def __init__(self, prompt: Callable[[str], bool] | None = None):
        self.prompt = prompt or self._terminal_prompt

    def authorize(self, action: Action, interactive: bool) -> tuple[bool, str]:
        risk = self.effective_risk(action)
        if risk == RiskLevel.LOW:
            return True, "low-risk action"
        if not interactive:
            return False, "approval required in non-interactive mode"
        allowed = self.prompt(
            f"Approve {risk.value}-risk action {action.tool} {action.arguments}? [y/N] "
        )
        return allowed, "approved by user" if allowed else "approval denied by user"

    def effective_risk(self, action: Action) -> RiskLevel:
        if action.tool != "shell.execute":
            return action.risk_level
        command = str(action.arguments.get("command", "")).strip()
        if any(command == prefix.strip() or command.startswith(prefix) for prefix in self.SAFE_SHELL_PREFIXES):
            return action.risk_level
        return RiskLevel.HIGH

    @staticmethod
    def _terminal_prompt(message: str) -> bool:
        return input(message).strip().lower() in {"y", "yes"}


def _resolve_inside(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path escapes workspace: {raw_path}")
    return resolved


def build_local_tool_registry(workspace: str | Path) -> ToolRegistry:
    root = Path(workspace).resolve()
    registry = ToolRegistry()

    def read_file(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments["path"]))
        if not path.is_file():
            return Observation("", False, error=f"not a file: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        return Observation("", True, output=text, evidence=(f"read:{path}",))

    def list_files(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        if not path.is_dir():
            return Observation("", False, error=f"not a directory: {path}")
        recursive = bool(arguments.get("recursive", False))
        entries = path.rglob("*") if recursive else path.iterdir()
        values = sorted(str(item.relative_to(root)) for item in entries)
        return Observation("", True, output="\n".join(values), evidence=(f"listed:{path}",))

    def search_text(arguments: dict) -> Observation:
        path = _resolve_inside(root, str(arguments.get("path", ".")))
        query = str(arguments["query"])
        matches: list[str] = []
        files = [path] if path.is_file() else path.rglob("*")
        for file_path in files:
            if not file_path.is_file() or ".git" in file_path.parts:
                continue
            try:
                for line_number, line in enumerate(
                    file_path.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if query in line:
                        matches.append(f"{file_path.relative_to(root)}:{line_number}:{line}")
            except OSError:
                continue
        return Observation(
            "",
            True,
            output="\n".join(matches),
            evidence=(f"search:{query}:{len(matches)}",),
        )

    def shell_execute(arguments: dict) -> Observation:
        command = str(arguments["command"])
        timeout = min(int(arguments.get("timeout", 60)), 300)
        completed = subprocess.run(
            shlex.split(command),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
        output = completed.stdout
        error = completed.stderr
        return Observation(
            "",
            completed.returncode == 0,
            output=output,
            error=error,
            evidence=(f"exit_code:{completed.returncode}",),
            exit_code=completed.returncode,
        )

    def validate_read(arguments: dict) -> None:
        _resolve_inside(root, str(arguments["path"]))

    def validate_list(arguments: dict) -> None:
        _resolve_inside(root, str(arguments.get("path", ".")))

    def validate_search(arguments: dict) -> None:
        _resolve_inside(root, str(arguments.get("path", ".")))
        if not str(arguments["query"]):
            raise ValueError("query must not be empty")

    def validate_shell(arguments: dict) -> None:
        if not str(arguments["command"]).strip():
            raise ValueError("command must not be empty")
        timeout = int(arguments.get("timeout", 60))
        if timeout < 1:
            raise ValueError("timeout must be at least 1")

    registry.register(
        "filesystem.read",
        read_file,
        validate_read,
        description="Read a UTF-8 text file inside the workspace.",
        toolset="filesystem",
        argument_contract={"path": "string"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "filesystem.list",
        list_files,
        validate_list,
        description="List files or directories inside the workspace.",
        toolset="filesystem",
        argument_contract={"path": "string (optional)", "recursive": "boolean (optional)"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "search.text",
        search_text,
        validate_search,
        description="Search workspace text files for an exact query string.",
        toolset="search",
        argument_contract={"query": "string", "path": "string (optional)"},
        default_risk=RiskLevel.LOW,
    )
    registry.register(
        "shell.execute",
        shell_execute,
        validate_shell,
        description="Execute a shell command in the workspace.",
        toolset="shell",
        argument_contract={"command": "string", "timeout": "integer (optional)"},
        default_risk=RiskLevel.LOW,
        side_effects=("command-dependent",),
    )
    return registry
