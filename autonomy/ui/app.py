from __future__ import annotations

import os
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, TextIO

from ..models import ActionRecipe, ConversationResponse, RecipeStatus, TerminationReason, jsonable
from ..procedure_skills import ProcedureSkillError, ProcedureSkillLibrary
from ..providers import ModelConfigStore, ProviderConfigurationError
from ..store import AutonomyStore
from ..toolsets import ToolsetConfigStore, ToolsetConfigurationError
from ..tools import ApprovalPolicy


class ConversationLike(Protocol):
    def handle_user_input(self, text: str) -> ConversationResponse:
        ...


class ShellLike(Protocol):
    workspace: Any
    db_path: Any
    max_steps: int
    conversation: ConversationLike
    output: TextIO

    def input_func(self, prompt: str) -> str:
        ...

    def _handle_command(self, line: str) -> bool:
        ...

    def _handle_candidate_skill_prompts(self, candidates: tuple[dict, ...]) -> None:
        ...

    def _handle_candidate_recipe_prompts(self, candidates: tuple[dict, ...]) -> None:
        ...


@dataclass(frozen=True)
class TUITheme:
    accent: str = "\033[38;5;45m"
    muted: str = "\033[38;5;245m"
    good: str = "\033[38;5;78m"
    warn: str = "\033[38;5;220m"
    bad: str = "\033[38;5;203m"
    reset: str = "\033[0m"


class AutonomyTUI:
    """Hermes-inspired terminal UI around the existing ConversationLoop.

    This is intentionally a presentation layer. It delegates slash commands and
    candidate approval prompts to the existing session shell and never executes
    tools directly.
    """

    PROMPT = "autonomy ui> "

    def __init__(
        self,
        shell: ShellLike,
        *,
        width: int | None = None,
        color: bool | None = None,
        store_factory: Callable[[Path | str], Any] = AutonomyStore,
        skill_library_factory: Callable[[Any, Any], ProcedureSkillLibrary] = ProcedureSkillLibrary,
    ):
        self.shell = shell
        self.output = shell.output
        self.width = width
        self.theme = TUITheme()
        self.color = self._detect_color() if color is None else color
        self.store_factory = store_factory
        self.skill_library_factory = skill_library_factory
        self.details_mode = "compact"
        self.turn_count = 0
        self.run_count = 0
        self.last_run_id: str | None = None
        self.last_run_termination: TerminationReason | None = None
        self._install_approval_panel()

    def run(self) -> int:
        self._print_startup()
        while True:
            try:
                self._write(self._prompt_status_rule())
                line = self.shell.input_func(self.PROMPT).strip()
            except EOFError:
                self._print_goodbye()
                return 0
            if not line:
                continue
            if line.startswith("/"):
                if line in {"/exit", "/quit"}:
                    self._print_goodbye()
                    return 0
                if line in {"/", "/?"}:
                    self._write(self._box("Command Palette", self._command_palette_lines()))
                    continue
                if line == "/details" or line.startswith("/details "):
                    self._write(self._handle_details_command(line))
                    continue
                should_continue = self.shell._handle_command(line)
                if not should_continue:
                    return 0
                continue
            self._run_conversation_turn(line)

    def _print_startup(self) -> None:
        self._write(self._box("Autonomy Workbench", self._startup_lines()))

    def _print_goodbye(self) -> None:
        self._write(
            self._box(
                "Session Closed",
                [
                    "AUTONOMY session summary",
                    f"turns:        {self.turn_count}",
                    f"agent runs:   {self.run_count}",
                    f"details mode: {self.details_mode}",
                    f"last run:     {self._last_run_summary()}",
                    f"workspace:    {self.shell.workspace}",
                    f"database:     {self.shell.db_path}",
                ],
                tone="muted",
            )
        )

    def _startup_lines(self) -> list[str]:
        return [
            *self._banner_lines(),
            "",
            "Session:",
            "  Runtime:  ConversationLoop -> AgentLoop -> ActionGateway",
            f"  Steps:    max {self.shell.max_steps} per agent run",
            f"  Detail:   {self.details_mode}",
            "",
            "Workspace:",
            f"  Root:     {self.shell.workspace}",
            f"  Journal:  {self.shell.db_path}",
            "",
            "Configuration:",
            f"  {self._model_status_line()}",
            f"  {self._toolset_status_line()}",
            "",
            "Boundaries:",
            "  UI never executes tools directly",
            "  Skills guide candidate generation; ActionGateway governs execution",
            "",
            "Review queues:",
            "  ProcedureSkill candidates and ActionRecipe candidates appear after agent runs",
            "",
            "Commands:",
            "  / or /? opens the command palette",
            "  /details compact|full changes run detail density",
            "  /help  /doctor  /inspect  /skills  /recipes  /tools  /exit",
        ]

    def _banner_lines(self) -> list[str]:
        width = self._current_width()
        if width >= 88:
            return [
                "    ___         __                                      ",
                "   /   | __  __/ /_____  ____  ____  ____ ___  __  __  ",
                "  / /| |/ / / / __/ __ \\/ __ \\/ __ \\/ __ `__ \\/ / / /  ",
                " / ___ / /_/ / /_/ /_/ / / / / /_/ / / / / / / /_/ /   ",
                "/_/  |_\\__,_/\\__/\\____/_/ /_/\\____/_/ /_/ /_/\\__, /    ",
                "                                             /____/     ",
                "conversation-first, self-governed AI workspace",
            ]
        if width >= 58:
            return [
                self._center_line("AUTONOMY"),
                self._center_line("conversation-first, self-governed AI workspace"),
                self._rule_line(),
            ]
        return [
            "AUTONOMY",
            "self-governed AI workspace",
        ]

    def _command_palette_lines(self) -> list[str]:
        return [
            "Core:",
            "  /help                 show session help",
            "  /doctor               run health checks",
            "  /inspect RUN_ID       inspect a saved run journal",
            "",
            "Workspace:",
            "  /workspace PATH       switch the workspace for future runs",
            "  /max-steps N          set the step limit for future runs",
            "",
            "Autonomy:",
            "  /skills               view ProcedureSkill entries",
            "  /recipes              view ActionRecipe entries",
            "  /tools                view toolset status",
            "  /details compact      show dashboard and Action trail only",
            "  /details full         also show run event timeline",
            "",
            "Session:",
            "  /exit                 leave the TUI",
            "  /quit                 leave the TUI",
        ]

    def _handle_details_command(self, line: str) -> str:
        parts = line.split()
        if len(parts) == 1:
            return self._box(
                "Details",
                [
                    f"current mode: {self.details_mode}",
                    "",
                    "/details compact   dashboard + Action trail",
                    "/details full      dashboard + Action trail + timeline",
                ],
            )
        if len(parts) == 2 and parts[1] in {"compact", "full"}:
            self.details_mode = parts[1]
            return self._status_line(f"details mode: {self.details_mode}", tone="good")
        return self._box(
            "Details",
            ["usage: /details compact", "       /details full"],
            tone="warn",
        )

    def _install_approval_panel(self) -> None:
        factory = getattr(self.shell, "agent_loop_factory", None)
        if not callable(factory):
            factory = getattr(self.shell.conversation, "agent_loop_factory", None)
        if not callable(factory):
            return

        def wrapped_factory(workspace, db_path):
            agent_loop = factory(workspace, db_path)
            action_gateway = getattr(agent_loop, "action_gateway", None)
            if action_gateway is not None:
                action_gateway.approval = ApprovalPolicy(prompt=self._approval_prompt)
            return agent_loop

        if hasattr(self.shell, "agent_loop_factory"):
            self.shell.agent_loop_factory = wrapped_factory
        self.shell.conversation.agent_loop_factory = wrapped_factory

    def _run_conversation_turn(self, text: str) -> None:
        self.turn_count += 1
        turn_number = self.turn_count
        self._write(self._box(f"You #{turn_number}", self._wrap_text(text), tone="muted"))
        try:
            response = self.shell.conversation.handle_user_input(text)
        except Exception as exc:  # pragma: no cover - exact model/provider errors vary
            self._write(self._box("Configuration", [f"error: {exc}"], tone="bad"))
            return

        self._remember_response(response)
        self._write(self._render_response(response, turn_number=turn_number))
        self._handle_candidate_skill_reviews(response.candidate_skills)
        self._handle_candidate_recipe_reviews(response.action_recipe_candidates)

    def _remember_response(self, response: ConversationResponse) -> None:
        if response.run_result is None:
            return
        self.run_count += 1
        self.last_run_id = response.run_result.run_id
        self.last_run_termination = response.run_result.termination

    def _render_response(self, response: ConversationResponse, *, turn_number: int | None = None) -> str:
        lines: list[str] = []
        route_lines = self._route_lines(response)
        if route_lines:
            lines.extend(route_lines)
            lines.append("")
        reply = response.reply.strip() or "(empty response)"
        lines.extend(self._wrap_text(reply))
        if response.run_result is not None:
            result = response.run_result
            lines.append("")
            lines.append(f"status:      {self._termination_status_label(result.termination)}")
            lines.extend(
                [
                    f"run_id:      {result.run_id}",
                    f"termination: {result.termination.value}",
                    f"steps:       {result.steps_executed}",
                    f"reason:      {result.reason}",
                ]
            )
            lines.append(f"next:        /inspect {result.run_id}")
            journal = self._journal_for_run(result.run_id)
            dashboard = self._run_dashboard_lines(journal)
            if dashboard:
                lines.append("")
                lines.append("Run dashboard:")
                lines.extend(f"  {line}" for line in dashboard)
            action_trail = self._action_trail_lines(journal)
            if action_trail:
                lines.append("")
                lines.append("Action trail:")
                lines.extend(f"  {line}" for line in action_trail)
            timeline = self._timeline_lines(journal)
            if timeline and self.details_mode == "full":
                lines.append("")
                lines.append("Run timeline:")
                lines.extend(f"  {line}" for line in timeline)
            elif timeline:
                lines.append("")
                lines.append("details: compact; use /details full to show the event timeline")
        if response.candidate_skills or response.action_recipe_candidates:
            lines.append("")
            lines.append(
                "review queue: "
                f"{len(response.candidate_skills)} ProcedureSkill candidate(s), "
                f"{len(response.action_recipe_candidates)} ActionRecipe candidate(s)"
            )
        title = "Task Result" if response.run_result is not None else "Conversation"
        if turn_number is not None:
            title = f"{title} #{turn_number}"
        tone = self._response_tone(response)
        return self._box(title, lines, tone=tone)

    def _response_tone(self, response: ConversationResponse) -> str:
        if response.run_result is None:
            return "accent"
        return self._termination_tone(response.run_result.termination)

    def _termination_tone(self, termination: TerminationReason) -> str:
        if termination is TerminationReason.ACHIEVED:
            return "good"
        if termination in {
            TerminationReason.BLOCKED,
            TerminationReason.NO_CANDIDATES,
            TerminationReason.MAX_STEPS_REACHED,
        }:
            return "warn"
        return "bad"

    def _termination_status_label(self, termination: TerminationReason) -> str:
        if termination is TerminationReason.ACHIEVED:
            return "completed"
        if termination in {
            TerminationReason.BLOCKED,
            TerminationReason.NO_CANDIDATES,
            TerminationReason.MAX_STEPS_REACHED,
        }:
            return "needs attention"
        return "stopped"

    def _route_lines(self, response: ConversationResponse) -> list[str]:
        decision = response.decision
        if decision is None:
            return []
        mode = getattr(decision.mode, "value", str(decision.mode))
        lines = [f"route: {mode}"]
        if decision.task_goal:
            lines.append(f"task goal: {decision.task_goal}")
        if decision.reason:
            lines.append(f"router reason: {decision.reason}")
        return lines

    def _journal_for_run(self, run_id: str) -> dict[str, Any] | None:
        try:
            journal = self.store_factory(self.shell.db_path).inspect_run(run_id)
        except Exception:
            return None
        return journal if isinstance(journal, dict) else None

    def _runtime_status_lines(self) -> list[str]:
        return [
            self._model_status_line(),
            self._toolset_status_line(),
        ]

    def _model_status_line(self) -> str:
        config_dir = getattr(self.shell, "config_dir", None)
        if config_dir is None:
            return "model:    unknown"
        try:
            configuration = ModelConfigStore(Path(config_dir)).load()
        except (ProviderConfigurationError, OSError) as exc:
            return f"model:    not configured ({exc})"
        return (
            f"model:    {configuration.provider}/{configuration.model} "
            f"({configuration.base_url})"
        )

    def _toolset_status_line(self) -> str:
        tool_config_dir = getattr(self.shell, "tool_config_dir", None)
        if tool_config_dir is None:
            return "toolsets: unknown"
        try:
            configuration = ToolsetConfigStore(Path(tool_config_dir)).load()
        except (ToolsetConfigurationError, OSError) as exc:
            return f"toolsets: invalid ({exc})"
        enabled = ", ".join(configuration.enabled_toolsets) or "none"
        disabled = len(configuration.disabled_tools)
        suffix = f"; {disabled} disabled tool(s)" if disabled else ""
        return f"toolsets: {enabled}{suffix}"

    def _prompt_status_rule(self) -> str:
        workspace_name = Path(self.shell.workspace).name or str(self.shell.workspace)
        pieces = [
            "Autonomy",
            self._model_status_compact(),
            self._toolset_status_compact(),
            f"max {self.shell.max_steps}",
            f"details {self.details_mode}",
            f"turn {self.turn_count}",
            self._session_mix_compact(),
            self._last_run_status_compact(),
            workspace_name,
        ]
        content = " · ".join(piece for piece in pieces if piece)
        width = max(48, self._current_width())
        if len(content) > width - 4:
            content = content[: max(1, width - 5)] + "…"
        padding = max(1, width - len(content) - 3)
        return self._status_line("─ " + content + " " + "─" * padding, tone="muted")

    def _model_status_compact(self) -> str:
        config_dir = getattr(self.shell, "config_dir", None)
        if config_dir is None:
            return "model unknown"
        try:
            configuration = ModelConfigStore(Path(config_dir)).load()
        except (ProviderConfigurationError, OSError):
            return "model unset"
        return f"{configuration.provider}/{configuration.model}"

    def _toolset_status_compact(self) -> str:
        tool_config_dir = getattr(self.shell, "tool_config_dir", None)
        if tool_config_dir is None:
            return "tools unknown"
        try:
            configuration = ToolsetConfigStore(Path(tool_config_dir)).load()
        except (ToolsetConfigurationError, OSError):
            return "tools invalid"
        enabled = tuple(configuration.enabled_toolsets)
        if not enabled:
            return "tools none"
        preview = ",".join(enabled[:3])
        if len(enabled) > 3:
            preview += f"+{len(enabled) - 3}"
        return f"tools {preview}"

    def _session_mix_compact(self) -> str:
        return f"runs {self.run_count}"

    def _last_run_status_compact(self) -> str:
        if self.last_run_id is None or self.last_run_termination is None:
            return ""
        return f"last {self.last_run_id} {self.last_run_termination.value}"

    def _last_run_summary(self) -> str:
        compact = self._last_run_status_compact()
        return compact.removeprefix("last ") if compact else "none"

    def _center_line(self, text: str) -> str:
        width = max(1, self._current_width() - 4)
        clipped = text if len(text) <= width else text[: max(1, width - 1)] + "…"
        padding = max(0, width - len(clipped))
        left = padding // 2
        return " " * left + clipped + " " * (padding - left)

    def _rule_line(self) -> str:
        width = max(1, self._current_width() - 4)
        return "─" * width

    def _run_dashboard_lines(self, journal: dict[str, Any] | None) -> list[str]:
        if not journal:
            return []
        events = journal.get("events", [])
        if not isinstance(events, list):
            return []
        summary: dict[str, Any] = {
            "ranked": None,
            "blocked": 0,
            "tool": "",
            "risk": "",
            "approval": "",
            "observation": "",
            "outcome": "",
            "skills": "",
            "learning": 0,
        }
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type", ""))
            payload = event.get("payload", {})
            if event_type == "skills_selected" and isinstance(payload, list):
                summary["skills"] = ", ".join(str(item) for item in payload[:3])
            elif event_type == "candidates_ranked" and isinstance(payload, list):
                summary["ranked"] = len(payload)
            elif event_type == "execution_candidates_blocked" and isinstance(payload, list):
                summary["blocked"] = int(summary["blocked"]) + len(payload)
            elif event_type == "action_selected" and isinstance(payload, dict):
                summary["tool"] = str(payload.get("tool", ""))
                summary["risk"] = str(payload.get("effective_risk_level", payload.get("risk_level", "")))
            elif event_type == "approval_decision" and isinstance(payload, dict):
                summary["approval"] = "allowed" if payload.get("allowed") else "denied"
            elif event_type == "observation" and isinstance(payload, dict):
                summary["observation"] = "succeeded" if payload.get("succeeded") else "failed"
            elif event_type == "outcome_evaluated" and isinstance(payload, dict):
                status = payload.get("goal_status", "")
                confidence = payload.get("confidence", "")
                summary["outcome"] = f"{status} ({confidence})" if confidence != "" else str(status)
            elif event_type in {"candidate_recipe_learned", "procedure_skill_candidate_created"}:
                summary["learning"] = int(summary["learning"]) + 1
        lines: list[str] = []
        if summary["skills"]:
            lines.append(f"skills: {summary['skills']}")
        if summary["ranked"] is not None:
            blocked = int(summary["blocked"])
            suffix = f"; {blocked} blocked by boundary" if blocked else ""
            lines.append(f"candidates: {summary['ranked']} ranked{suffix}")
        if summary["tool"]:
            risk = f" [{summary['risk']}]" if summary["risk"] else ""
            lines.append(f"selected action: {summary['tool']}{risk}")
        if summary["approval"]:
            lines.append(f"approval: {summary['approval']}")
        if summary["observation"]:
            lines.append(f"observation: {summary['observation']}")
        if summary["outcome"]:
            lines.append(f"outcome: {summary['outcome']}")
        if summary["learning"]:
            lines.append(f"learning: {summary['learning']} candidate update(s)")
        return lines

    def _action_trail_lines(self, journal: dict[str, Any] | None) -> list[str]:
        if not journal:
            return []
        events = journal.get("events", [])
        if not isinstance(events, list):
            return []
        by_step: dict[Any, dict[str, str]] = {}
        order: list[Any] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            step = event.get("step", "?")
            event_type = str(event.get("event_type", ""))
            payload = event.get("payload", {})
            if step not in by_step:
                by_step[step] = {}
                order.append(step)
            row = by_step[step]
            if event_type == "action_selected" and isinstance(payload, dict):
                tool = str(payload.get("tool", ""))
                risk = str(payload.get("effective_risk_level", payload.get("risk_level", "")))
                purpose = str(payload.get("purpose", ""))
                risk_label = f" [{risk}]" if risk else ""
                purpose_label = f" · {purpose}" if purpose else ""
                row["action"] = f"{tool}{risk_label}{purpose_label}"
            elif event_type == "approval_decision" and isinstance(payload, dict):
                row["approval"] = "approval allowed" if payload.get("allowed") else "approval denied"
            elif event_type == "observation" and isinstance(payload, dict):
                row["observation"] = "observation succeeded" if payload.get("succeeded") else "observation failed"
            elif event_type == "outcome_evaluated" and isinstance(payload, dict):
                status = str(payload.get("goal_status", ""))
                confidence = payload.get("confidence", "")
                if confidence != "":
                    row["outcome"] = f"outcome {status} ({confidence})"
                elif status:
                    row["outcome"] = f"outcome {status}"
            elif event_type == "execution_candidates_blocked" and isinstance(payload, list) and payload:
                row["blocked"] = f"{len(payload)} candidate(s) blocked"

        lines: list[str] = []
        for step in order:
            row = by_step.get(step, {})
            if not row.get("action") and not row.get("blocked"):
                continue
            parts = [
                row.get("blocked", ""),
                row.get("action", ""),
                row.get("approval", ""),
                row.get("observation", ""),
                row.get("outcome", ""),
            ]
            detail = " -> ".join(part for part in parts if part)
            if detail:
                lines.append(f"step {step}: {detail}")
        return lines[-8:]

    def _timeline_lines(self, journal: dict[str, Any] | None) -> list[str]:
        if not journal:
            return []
        events = journal.get("events", [])
        if not isinstance(events, list):
            return []
        lines: list[str] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            summary = self._event_summary(event)
            if summary:
                step = event.get("step", "?")
                lines.append(f"step {step}: {summary}")
        return lines[-12:]

    def _event_summary(self, event: dict[str, Any]) -> str:
        event_type = str(event.get("event_type", ""))
        payload = event.get("payload", {})
        if event_type == "run_started" and isinstance(payload, dict):
            interface = payload.get("interface", "")
            provider = payload.get("model_provider", "")
            model = payload.get("model", "")
            model_label = f" · {provider}/{model}" if provider or model else ""
            return f"run started via {interface or 'unknown'}{model_label}"
        if event_type == "project_context_loaded" and isinstance(payload, dict):
            source = payload.get("source", "")
            chars = payload.get("chars", 0)
            return f"project context loaded: {source} ({chars} chars)"
        if event_type == "skills_considered" and isinstance(payload, list):
            return f"procedure skills considered: {len(payload)}"
        if event_type == "skills_selected" and isinstance(payload, list):
            names = ", ".join(str(item) for item in payload[:3])
            return f"procedure skills selected: {names or 'none'}"
        if event_type == "skills_loaded" and isinstance(payload, list):
            names = ", ".join(str(item.get("name", "")) for item in payload[:3] if isinstance(item, dict))
            return f"procedure skills loaded: {names or len(payload)}"
        if event_type == "action_intents_generated" and isinstance(payload, list):
            return f"action intents generated: {len(payload)}"
        if event_type == "candidates_penalized" and isinstance(payload, list):
            return f"candidate penalties: {len(payload)}"
        if event_type == "candidates_ranked" and isinstance(payload, list):
            first = self._candidate_tool_label(payload[0]) if payload else ""
            suffix = f" · top: {first}" if first else ""
            return f"candidates ranked: {len(payload)}{suffix}"
        if event_type == "execution_candidates_blocked" and isinstance(payload, list):
            return f"execution boundary blocked: {len(payload)} candidate(s)"
        if event_type == "action_selected" and isinstance(payload, dict):
            tool = payload.get("tool", "")
            risk = payload.get("effective_risk_level", payload.get("risk_level", ""))
            purpose = payload.get("purpose", "")
            suffix = f" · {purpose}" if purpose else ""
            return f"action selected: {tool} [{risk}]{suffix}"
        if event_type == "approval_decision" and isinstance(payload, dict):
            allowed = "allowed" if payload.get("allowed") else "denied"
            reason = payload.get("reason", "")
            return f"approval {allowed}: {reason}"
        if event_type == "observation" and isinstance(payload, dict):
            status = "succeeded" if payload.get("succeeded") else "failed"
            error = payload.get("error", "")
            evidence = payload.get("evidence", ())
            detail = error or (evidence[0] if isinstance(evidence, list | tuple) and evidence else "")
            return f"observation {status}{(': ' + str(detail)) if detail else ''}"
        if event_type == "outcome_evaluated" and isinstance(payload, dict):
            status = payload.get("goal_status", "")
            reason = payload.get("reason", "")
            confidence = payload.get("confidence", "")
            return f"outcome: {status} ({confidence}) · {reason}"
        if event_type == "candidate_recipe_learned" and isinstance(payload, dict):
            recipe = payload.get("recipe", {})
            recipe_id = recipe.get("id", "") if isinstance(recipe, dict) else ""
            created = "created" if payload.get("created") else "updated"
            return f"ActionRecipe candidate {created}: {recipe_id}"
        if event_type == "learning_review" and isinstance(payload, dict):
            proposal_type = payload.get("proposal_type", "")
            status = payload.get("status", "")
            return f"learning review: {proposal_type} / {status}"
        if event_type == "procedure_skill_candidate_created" and isinstance(payload, dict):
            return f"ProcedureSkill candidate created: {payload.get('name', '')}"
        if event_type == "run_finished" and isinstance(payload, dict):
            return f"run finished: {payload.get('termination', '')}"
        if event_type.endswith("_error") and isinstance(payload, dict):
            return f"{event_type}: {payload.get('error', '')}"
        return ""

    def _candidate_tool_label(self, candidate: Any) -> str:
        if not isinstance(candidate, dict):
            return ""
        actions = candidate.get("actions", ())
        if not isinstance(actions, list | tuple) or not actions:
            return ""
        action = actions[0]
        if not isinstance(action, dict):
            return ""
        return str(action.get("tool", ""))

    def _handle_candidate_skill_reviews(self, candidates: tuple[dict, ...]) -> None:
        if not candidates:
            return
        store = self.store_factory(self.shell.db_path)
        library = self.skill_library_factory(self.shell.workspace, store)
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id", ""))
            self._write(
                self._box(
                    "Skill Review",
                    [
                        "Candidate ProcedureSkill created",
                        f"id:         {candidate_id}",
                        f"name:       {candidate.get('name', '')}",
                        f"source run: {candidate.get('source_run_id', '')}",
                        "",
                        "[v] view  [a] approve  [r] reject  [enter] later",
                    ],
                    tone="warn",
                )
            )
            while True:
                choice = self._prompt_choice("skill review> ")
                try:
                    if choice == "":
                        self._write(self._status_line("candidate kept for later", tone="muted"))
                        break
                    if choice in {"v", "view"}:
                        self._write(
                            self._box(
                                "Skill Draft",
                                library.view_candidate(candidate_id).raw_content.splitlines(),
                            )
                        )
                        continue
                    if choice in {"a", "approve", "y"}:
                        approved = library.approve_candidate(candidate_id)
                        self._write(self._status_line(f"approved: {approved.summary.name}", tone="good"))
                        break
                    if choice in {"r", "reject", "n"}:
                        library.reject_candidate(candidate_id)
                        self._write(self._status_line(f"rejected: {candidate_id}", tone="bad"))
                        break
                    self._write(self._status_line("choose v, a, r, or enter", tone="warn"))
                except (KeyError, FileExistsError, ProcedureSkillError) as exc:
                    self._write(self._box("Skill Review Error", [str(exc)], tone="bad"))
                    break

    def _handle_candidate_recipe_reviews(self, candidates: tuple[dict, ...]) -> None:
        if not candidates:
            return
        store = self.store_factory(self.shell.db_path)
        for candidate in candidates[:3]:
            recipe_id = str(candidate.get("id", ""))
            action_template = candidate.get("action_template", {})
            if not isinstance(action_template, dict):
                action_template = {}
            self._write(
                self._box(
                    "ActionRecipe Review",
                    [
                        "Candidate ActionRecipe learned",
                        f"id:       {recipe_id}",
                        f"tool:     {action_template.get('tool', '')}",
                        f"purpose:  {action_template.get('purpose', candidate.get('intent', ''))}",
                        f"evidence: {candidate.get('evidence_count', 0)} successful outcomes",
                        "",
                        "[v] view  [a] activate  [d] disable  [enter] later",
                    ],
                    tone="warn",
                )
            )
            while True:
                choice = self._prompt_choice("recipe review> ")
                try:
                    if choice == "":
                        self._write(self._status_line("candidate kept for later", tone="muted"))
                        break
                    if choice in {"v", "view"}:
                        self._write(
                            self._box(
                                "ActionRecipe",
                                json.dumps(
                                    jsonable(self._recipe_by_id(store, recipe_id)),
                                    indent=2,
                                    sort_keys=True,
                                ).splitlines(),
                            )
                        )
                        continue
                    if choice in {"a", "activate", "y"}:
                        store.set_recipe_state(
                            recipe_id,
                            status=RecipeStatus.ACTIVE,
                            enabled=True,
                        )
                        self._write(self._status_line(f"activated: {recipe_id}", tone="good"))
                        break
                    if choice in {"d", "disable"}:
                        store.set_recipe_state(recipe_id, enabled=False)
                        self._write(self._status_line(f"disabled: {recipe_id}", tone="bad"))
                        break
                    self._write(self._status_line("choose v, a, d, or enter", tone="warn"))
                except KeyError as exc:
                    self._write(self._box("ActionRecipe Review Error", [str(exc)], tone="bad"))
                    break

    @staticmethod
    def _recipe_by_id(store: Any, recipe_id: str) -> ActionRecipe:
        for recipe in store.list_recipes():
            if recipe.id == recipe_id:
                return recipe
        raise KeyError(f"unknown recipe: {recipe_id}")

    def _prompt_choice(self, prompt: str) -> str:
        try:
            return self.shell.input_func(prompt).strip().lower()
        except EOFError:
            return ""

    def _approval_prompt(self, message: str) -> bool:
        display_message = message.strip()
        if display_message.endswith(" [y/N]"):
            display_message = display_message[: -len(" [y/N]")].rstrip()
        self._write(
            self._box(
                "Approval Required",
                [
                    display_message,
                    "",
                    "[a] approve  [d] deny  [enter] deny",
                ],
                tone="warn",
            )
        )
        while True:
            choice = self._prompt_choice("approval> ")
            if choice in {"a", "approve", "y", "yes"}:
                self._write(self._status_line("approved", tone="good"))
                return True
            if choice in {"", "d", "deny", "n", "no"}:
                self._write(self._status_line("denied", tone="bad"))
                return False
            self._write(self._status_line("choose a, d, or enter", tone="warn"))

    def _box(self, title: str, lines: list[str], *, tone: str = "accent") -> str:
        width = max(48, self._current_width())
        inner_width = width - 4
        title_text = f" {title} "
        top = "╭" + title_text + "─" * max(0, width - 2 - len(title_text)) + "╮"
        bottom = "╰" + "─" * (width - 2) + "╯"
        body = []
        for line in lines or [""]:
            for wrapped in self._wrap_text(line, inner_width):
                body.append("│ " + wrapped.ljust(inner_width) + " │")
        rendered = "\n".join([top, *body, bottom])
        if not self.color:
            return rendered
        color = getattr(self.theme, tone, self.theme.accent)
        return f"{color}{rendered}{self.theme.reset}"

    def _status_line(self, text: str, *, tone: str = "accent") -> str:
        if not self.color:
            return text
        color = getattr(self.theme, tone, self.theme.accent)
        return f"{color}{text}{self.theme.reset}"

    def _wrap_text(self, text: str, width: int | None = None) -> list[str]:
        width = width or max(44, self._current_width() - 4)
        lines: list[str] = []
        for raw in text.splitlines() or [""]:
            if not raw:
                lines.append("")
                continue
            current = raw
            while len(current) > width:
                split_at = current.rfind(" ", 0, width + 1)
                if split_at <= 0:
                    split_at = width
                lines.append(current[:split_at].rstrip())
                current = current[split_at:].lstrip()
            lines.append(current)
        return lines

    def _current_width(self) -> int:
        if self.width:
            return self.width
        return min(120, max(64, shutil.get_terminal_size((88, 24)).columns))

    def _detect_color(self) -> bool:
        return (
            hasattr(self.output, "isatty")
            and self.output.isatty()
            and os.environ.get("NO_COLOR") is None
        )

    def _write(self, message: str) -> None:
        print(message, file=self.output)
