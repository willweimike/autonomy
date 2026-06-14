from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..models import Action, ActionIntent, Observation, RiskLevel
from ..toolsets import ToolsetConfiguration


ToolHandler = Callable[[dict], Observation]
ToolValidator = Callable[[dict], None]
ToolAvailabilityCheck = Callable[[], tuple[bool, str]]


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
    availability_check: ToolAvailabilityCheck | None = field(
        repr=False,
        compare=False,
        default=None,
    )

    @property
    def summary(self) -> dict:
        available, unavailable_reason = self.availability
        return {
            "name": self.name,
            "description": self.description,
            "toolset": self.toolset,
            "argument_contract": self.argument_contract,
            "default_risk": self.default_risk.value,
            "side_effects": self.side_effects,
            "available": available,
            "unavailable_reason": unavailable_reason,
        }

    @property
    def availability(self) -> tuple[bool, str]:
        if not self.availability_check:
            return True, ""
        return self.availability_check()


class ToolRegistry:
    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}
        self._cleanup_callbacks: list[Callable[[], None]] = []

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
        availability_check: ToolAvailabilityCheck | None = None,
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
            availability_check=availability_check,
        )

    def register_cleanup(self, callback: Callable[[], None]) -> None:
        self._cleanup_callbacks.append(callback)

    def spec(self, tool_name: str) -> ToolSpec:
        if tool_name not in self._specs:
            raise KeyError(f"unknown tool: {tool_name}")
        return self._specs[tool_name]

    def filter_by_toolsets(
        self,
        configuration: ToolsetConfiguration,
        *,
        require_available: bool = True,
    ) -> "ToolRegistry":
        configuration.validate()
        enabled = configuration.enabled_set
        disabled_tools = configuration.disabled_tool_set
        filtered = ToolRegistry()
        for spec in self._specs.values():
            if spec.toolset not in enabled or spec.name in disabled_tools:
                continue
            available, _ = spec.availability
            if require_available and not available:
                continue
            filtered.register(
                spec.name,
                spec.handler,
                spec.validator,
                description=spec.description,
                toolset=spec.toolset,
                argument_contract=spec.argument_contract,
                default_risk=spec.default_risk,
                side_effects=spec.side_effects,
                availability_check=spec.availability_check,
            )
        filtered._cleanup_callbacks = list(self._cleanup_callbacks)
        return filtered

    def tool_statuses(self) -> dict[str, dict]:
        statuses: dict[str, dict] = {}
        for name, spec in self._specs.items():
            available, unavailable_reason = spec.availability
            statuses[name] = {
                "toolset": spec.toolset,
                "available": available,
                "unavailable_reason": unavailable_reason,
            }
        return statuses

    def model_specs(self) -> list[dict]:
        specs: list[dict] = []
        for name in sorted(self._specs):
            spec = self._specs[name]
            specs.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "toolset": spec.toolset,
                    "argument_contract": dict(spec.argument_contract),
                    "risk_level": spec.default_risk.value,
                    "side_effects": list(spec.side_effects),
                }
            )
        return specs

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
            evidence_strength=intent.evidence_strength,
            recipe_id=intent.recipe_id,
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

    def close(self) -> None:
        callbacks = list(reversed(self._cleanup_callbacks))
        self._cleanup_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                continue


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
        target = ""
        if "path" in action.arguments:
            target = f" path={action.arguments.get('path')}"
        allowed = self.prompt(
            f"Approve {risk.value}-risk action {action.tool}{target} purpose={action.purpose!r}? [y/N] "
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
