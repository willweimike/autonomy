from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Callable, TextIO

from .action_gateway import ActionGateway
from .agent_loop import AgentLoop
from .conversation import ConversationLoop
from .conversation_responder import (
    ConversationResponder,
    MissingModelConversationResponder,
    ModelConversationResponder,
)
from .model import AutonomyModel, ModelClientError
from .models import ActionRecipe, RecipeStatus, jsonable
from .procedure_skills import ProcedureSkillError, ProcedureSkillLibrary
from .project_context import load_project_context
from .providers import (
    PROVIDER_SPECS,
    ModelConfiguration,
    ModelConfigStore,
    ProviderConfigurationError,
    create_provider,
)
from .recipes import RecipeEngine
from .selection import CandidateSelector
from .skill_curator import CuratorDaemon, SkillCurator
from .storage import (
    workspace_autonomy_home,
    workspace_db_path,
)
from .store import AutonomyStore
from .tools import ApprovalPolicy, build_local_tool_registry
from .toolsets import (
    ToolsetConfiguration,
    ToolsetConfigStore,
    ToolsetConfigurationError,
    toolset_catalog_status,
)
from .outcome import ModelAssistedOutcomeEvaluator


_BRACKETED_PASTE_PATTERN = re.compile(r"\x1b\[\s*200~|\x1b\[\s*201~")


def default_db_path() -> Path:
    return Path(os.environ.get("AUTONOMY_DB", workspace_db_path())).expanduser()


def default_model_config_dir(workspace: Path | None = None) -> Path:
    return workspace_autonomy_home(workspace)


def default_toolset_config_dir(workspace: Path | None = None) -> Path:
    return workspace_autonomy_home(workspace)


def _model_config_dir_for(workspace: Path) -> Path:
    try:
        return default_model_config_dir(workspace)
    except TypeError:
        return default_model_config_dir()


def _toolset_config_dir_for(workspace: Path) -> Path:
    try:
        return default_toolset_config_dir(workspace)
    except TypeError:
        return default_toolset_config_dir()


def _db_path_for(workspace: Path, explicit_db: Path | None) -> Path:
    return explicit_db.expanduser() if explicit_db else workspace_db_path(workspace)


def _prepare_workspace_storage(workspace: Path) -> None:
    workspace_autonomy_home(workspace).mkdir(parents=True, exist_ok=True)


def build_agent_loop(
    workspace: Path,
    db_path: Path,
    *,
    config_dir: Path | None = None,
    tool_config_dir: Path | None = None,
) -> AgentLoop:
    store = AutonomyStore(db_path)
    config_store = ModelConfigStore(config_dir or _model_config_dir_for(workspace))
    provider = create_provider(config_store.load(), config_store)
    model = AutonomyModel.from_provider(provider)
    procedure_skills = ProcedureSkillLibrary(workspace, store)
    toolsets = ToolsetConfigStore(tool_config_dir or _toolset_config_dir_for(workspace)).load()
    agent_loop_ref: dict[str, AgentLoop] = {}

    def delegate_runner(goal, max_steps, parent_context):
        return agent_loop_ref["loop"].delegate_child(goal, max_steps, parent_context)

    tools = build_local_tool_registry(
        workspace,
        toolsets,
        delegate_runner=delegate_runner,
    )
    action_gateway = ActionGateway(
        tools=tools,
        store=store,
        approval=ApprovalPolicy(),
    )
    agent_loop = AgentLoop(
        model=model,
        action_gateway=action_gateway,
        outcome_evaluator=ModelAssistedOutcomeEvaluator(model),
        store=store,
        selector=CandidateSelector(beam_width=3),
        recipes=RecipeEngine(store),
        procedure_skills=procedure_skills,
        curator_daemon=CuratorDaemon(SkillCurator(procedure_skills, store)),
        project_context=load_project_context(workspace),
    )
    agent_loop_ref["loop"] = agent_loop
    return agent_loop


class SessionShell:
    PROMPT = "autonomy> "

    def __init__(
        self,
        *,
        workspace: Path,
        db_path: Path,
        max_steps: int,
        config_dir: Path | None = None,
        tool_config_dir: Path | None = None,
        input_func: Callable[[str], str] = input,
        output: TextIO = sys.stdout,
        agent_loop_factory: Callable[[Path, Path], AgentLoop] | None = None,
        responder: ConversationResponder | None = None,
    ):
        self.workspace = workspace.resolve()
        self.db_path = db_path
        self.max_steps = max_steps
        self.config_dir = config_dir or _model_config_dir_for(self.workspace)
        self.tool_config_dir = tool_config_dir or config_dir or _toolset_config_dir_for(self.workspace)
        self.input_func = input_func
        self.output = output
        self.agent_loop_factory = agent_loop_factory or self._build_agent_loop
        self.responder = responder or self._build_conversation_responder()
        self.conversation = ConversationLoop(
            workspace=self.workspace,
            db_path=self.db_path,
            max_steps=self.max_steps,
            agent_loop_factory=self.agent_loop_factory,
            responder=self.responder,
        )

    def run(self) -> int:
        self._print_startup()
        while True:
            try:
                line = self.input_func(self.PROMPT).strip()
            except EOFError:
                self._write("bye")
                return 0
            if not line:
                continue
            if line.startswith("/"):
                should_continue = self._handle_command(line)
                if not should_continue:
                    return 0
                continue
            self._run_goal(line)

    def _build_agent_loop(self, workspace: Path, db_path: Path) -> AgentLoop:
        return build_agent_loop(
            workspace,
            db_path,
            config_dir=self.config_dir,
            tool_config_dir=self.tool_config_dir,
        )

    def _build_conversation_responder(self):
        try:
            config_store = ModelConfigStore(self.config_dir)
            provider = create_provider(config_store.load(), config_store)
            model = AutonomyModel.from_provider(provider)
            return ModelConversationResponder(model)
        except (ProviderConfigurationError, ValueError) as exc:
            error = ProviderConfigurationError(str(exc))
            return MissingModelConversationResponder(error)

    def _print_startup(self) -> None:
        self._write("Autonomy interactive session")
        self._write(f"workspace: {self.workspace}")
        self._write(f"database: {self.db_path}")
        self._write(f"max_steps: {self.max_steps}")
        try:
            configuration = ModelConfigStore(self.config_dir).load()
            self._write(
                f"model: {configuration.provider}/{configuration.model} ({configuration.base_url})"
            )
        except ProviderConfigurationError as exc:
            self._write(f"model: not configured ({exc})")
        self._write("type /help for commands; /exit to quit")

    def _handle_command(self, line: str) -> bool:
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            self._write(f"command error: {exc}")
            return True
        if not parts:
            return True
        command = parts[0]
        arguments = parts[1:]
        if command in {"/exit", "/quit"}:
            self._write("bye")
            return False
        if command == "/help":
            self._write(
                "\n".join(
                    [
                        "Commands:",
                        "  /help",
                        "  /exit | /quit",
                        "  /doctor",
                        "  /inspect RUN_ID",
                        "  /workspace PATH",
                        "  /max-steps N",
                        "  /skills",
                        "  /recipes  (ActionRecipe view)",
                        "  /recipes activate RECIPE_ID",
                        "  /recipes disable RECIPE_ID",
                        "  /tools",
                    ]
                )
            )
            return True
        if command == "/doctor":
            with redirect_stdout(self.output):
                doctor(
                    self.db_path,
                    ModelConfigStore(self.config_dir),
                    self.workspace,
                    ToolsetConfigStore(self.tool_config_dir),
                )
            return True
        if command == "/inspect":
            if len(arguments) != 1:
                self._write("usage: /inspect RUN_ID")
                return True
            try:
                payload = AutonomyStore(self.db_path).inspect_run(arguments[0])
                self._write(json.dumps(payload, indent=2, sort_keys=True))
            except KeyError as exc:
                self._write(f"inspect error: {exc}")
            return True
        if command == "/workspace":
            if len(arguments) != 1:
                self._write("usage: /workspace PATH")
                return True
            path = Path(arguments[0]).expanduser()
            self.workspace = path.resolve()
            self.conversation.set_workspace(self.workspace)
            self._write(f"workspace: {self.workspace}")
            return True
        if command == "/max-steps":
            if len(arguments) != 1:
                self._write("usage: /max-steps N")
                return True
            try:
                value = int(arguments[0])
                if value < 1:
                    raise ValueError
            except ValueError:
                self._write("max-steps must be a positive integer")
                return True
            self.max_steps = value
            self.conversation.set_max_steps(value)
            self._write(f"max_steps: {self.max_steps}")
            return True
        if command == "/skills":
            self._handle_skills_command(arguments)
            return True
        if command == "/recipes":
            self._handle_recipes_command(arguments)
            return True
        if command == "/tools":
            self._handle_tools_command(arguments)
            return True
        self._write(f"unknown command: {command}")
        return True

    def _run_goal(self, goal: str) -> None:
        try:
            response = self.conversation.handle_user_input(goal)
        except (ProviderConfigurationError, ValueError) as exc:
            self._write(f"configuration error: {exc}")
            return
        self._write(response.reply)
        self._handle_candidate_skill_prompts(response.candidate_skills)
        self._handle_candidate_recipe_prompts(response.action_recipe_candidates)

    def _handle_skills_command(self, arguments: list[str]) -> None:
        registry = build_local_tool_registry(
            self.workspace,
            ToolsetConfigStore(self.tool_config_dir).load(),
        )
        library = ProcedureSkillLibrary(self.workspace, AutonomyStore(self.db_path))
        try:
            if not arguments:
                self._write(
                    json.dumps(
                        jsonable(library.index(registry.names, include_disabled=True)),
                        indent=2,
                        sort_keys=True,
                    )
                )
            elif arguments[0] == "candidates" and len(arguments) == 1:
                self._write(json.dumps(library.list_candidates(), indent=2, sort_keys=True))
            elif arguments[0] == "install-bundled":
                installed = library.install_bundled(arguments[1:] or None)
                self._write(json.dumps(jsonable(installed), indent=2, sort_keys=True))
            elif arguments[0] == "install-clawhub" and len(arguments) == 2:
                installed = library.install_clawhub(arguments[1])
                self._write(json.dumps(jsonable(installed), indent=2, sort_keys=True))
            elif arguments[0] == "install-hermes" and len(arguments) == 2:
                installed = library.install_hermes(arguments[1])
                self._write(json.dumps(jsonable(installed), indent=2, sort_keys=True))
            elif arguments[0] == "view-candidate" and len(arguments) == 2:
                self._write(library.view_candidate(arguments[1]).raw_content)
            elif arguments[0] == "approve" and len(arguments) == 2:
                approved = library.approve_candidate(arguments[1])
                self._write(json.dumps(jsonable(approved.summary), indent=2, sort_keys=True))
            elif arguments[0] == "reject" and len(arguments) == 2:
                self._write(json.dumps(library.reject_candidate(arguments[1]), indent=2, sort_keys=True))
            else:
                self._write(
                    "usage: /skills [candidates|install-bundled [SKILL_NAME...]|install-clawhub SPEC|install-hermes SPEC|view-candidate CANDIDATE_ID|approve CANDIDATE_ID|reject CANDIDATE_ID]"
                )
        except (KeyError, FileExistsError, ProcedureSkillError, ToolsetConfigurationError) as exc:
            self._write(f"skill error: {exc}")

    def _handle_recipes_command(self, arguments: list[str]) -> None:
        store = AutonomyStore(self.db_path)
        try:
            if not arguments or arguments == ["list"]:
                self._write(
                    json.dumps(
                        jsonable(store.list_recipes()),
                        indent=2,
                        sort_keys=True,
                    )
                )
                return
            if arguments[0] == "activate" and len(arguments) == 2:
                store.set_recipe_state(
                    arguments[1],
                    status=RecipeStatus.ACTIVE,
                    enabled=True,
                )
                self._write(f"activated: {arguments[1]}")
                return
            if arguments[0] == "disable" and len(arguments) == 2:
                store.set_recipe_state(arguments[1], enabled=False)
                self._write(f"disabled: {arguments[1]}")
                return
            self._write("usage: /recipes [list|activate RECIPE_ID|disable RECIPE_ID]")
        except KeyError as exc:
            self._write(f"recipe error: {exc}")

    def _handle_tools_command(self, arguments: list[str]) -> None:
        store = ToolsetConfigStore(self.tool_config_dir)
        try:
            if not arguments or arguments[0] in {"list", "status"}:
                configuration = store.load()
                registry = build_local_tool_registry(
                    self.workspace,
                    configuration,
                    require_available=False,
                )
                try:
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
                finally:
                    registry.close()
                return
            if arguments[0] == "enable" and len(arguments) == 2:
                self._write(json.dumps(store.enable(arguments[1]).as_document(), indent=2, sort_keys=True))
                return
            if arguments[0] == "disable" and len(arguments) == 2:
                self._write(json.dumps(store.disable(arguments[1]).as_document(), indent=2, sort_keys=True))
                return
            self._write("usage: /tools [list|status|enable TOOLSET|disable TOOLSET]")
        except ToolsetConfigurationError as exc:
            self._write(f"toolset error: {exc}")

    def _handle_candidate_skill_prompts(self, candidates: tuple[dict, ...]) -> None:
        if not candidates:
            return
        library = ProcedureSkillLibrary(self.workspace, AutonomyStore(self.db_path))
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id", ""))
            self._write(
                "\n".join(
                    [
                        "Candidate Skill created:",
                        f"- id: {candidate_id}",
                        f"- name: {candidate.get('name', '')}",
                        f"- source run: {candidate.get('source_run_id', '')}",
                        "",
                        "[v] view  [y] approve  [n] reject  [enter] later",
                    ]
                )
            )
            while True:
                choice = self.input_func("skill> ").strip().lower()
                try:
                    if choice == "":
                        self._write("candidate kept for later")
                        break
                    if choice == "v":
                        self._write(library.view_candidate(candidate_id).raw_content)
                        continue
                    if choice == "y":
                        approved = library.approve_candidate(candidate_id)
                        self._write(f"approved: {approved.summary.name}")
                        break
                    if choice == "n":
                        library.reject_candidate(candidate_id)
                        self._write(f"rejected: {candidate_id}")
                        break
                    self._write("choose v, y, n, or enter")
                except (KeyError, FileExistsError, ProcedureSkillError) as exc:
                    self._write(f"skill error: {exc}")
                    break

    def _handle_candidate_recipe_prompts(self, candidates: tuple[dict, ...]) -> None:
        if not candidates:
            return
        store = AutonomyStore(self.db_path)
        for candidate in candidates[:3]:
            recipe_id = str(candidate.get("id", ""))
            action_template = candidate.get("action_template", {})
            if not isinstance(action_template, dict):
                action_template = {}
            self._write(
                "\n".join(
                    [
                        "Candidate ActionRecipe learned:",
                        f"- id: {recipe_id}",
                        f"- tool: {action_template.get('tool', '')}",
                        f"- purpose: {action_template.get('purpose', candidate.get('intent', ''))}",
                        f"- evidence: {candidate.get('evidence_count', 0)} successful outcomes",
                        "",
                        "[v] view  [y] activate  [d] disable  [enter] later",
                    ]
                )
            )
            while True:
                choice = self.input_func("recipe> ").strip().lower()
                try:
                    if choice == "":
                        self._write("candidate kept for later")
                        break
                    if choice == "v":
                        self._write(
                            json.dumps(
                                jsonable(self._recipe_by_id(store, recipe_id)),
                                indent=2,
                                sort_keys=True,
                            )
                        )
                        continue
                    if choice == "y":
                        store.set_recipe_state(
                            recipe_id,
                            status=RecipeStatus.ACTIVE,
                            enabled=True,
                        )
                        self._write(f"activated: {recipe_id}")
                        break
                    if choice == "d":
                        store.set_recipe_state(recipe_id, enabled=False)
                        self._write(f"disabled: {recipe_id}")
                        break
                    self._write("choose v, y, d, or enter")
                except KeyError as exc:
                    self._write(f"recipe error: {exc}")
                    break

    @staticmethod
    def _recipe_by_id(store: AutonomyStore, recipe_id: str) -> ActionRecipe:
        for recipe in store.list_recipes():
            if recipe.id == recipe_id:
                return recipe
        raise KeyError(f"unknown recipe: {recipe_id}")

    def _write(self, message: str) -> None:
        print(message, file=self.output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autonomy",
        description="Start the Autonomy TUI when no subcommand is provided.",
    )
    parser.add_argument("--db", type=Path, default=None)
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser("chrome-host")

    discord_bot = subparsers.add_parser("discord-bot")
    discord_bot.add_argument("--workspace", type=Path, default=Path.cwd())
    discord_bot.add_argument("--max-steps", type=int, default=12)

    run = subparsers.add_parser("run")
    run.add_argument("goal")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--max-steps", type=int, default=12)
    run.add_argument("--non-interactive", action="store_true")

    tui = subparsers.add_parser("tui")
    tui.add_argument("--workspace", type=Path, default=Path.cwd())
    tui.add_argument("--max-steps", type=int, default=12)

    model = subparsers.add_parser("model")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    setup = model_sub.add_parser("setup")
    setup.add_argument("provider", nargs="?", choices=sorted(PROVIDER_SPECS))

    subparsers.add_parser("doctor")
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("run_id")

    recipes = subparsers.add_parser(
        "recipes",
        description="Commands for learned ActionRecipe entries.",
    )
    recipes_sub = recipes.add_subparsers(dest="recipes_command", required=True)
    recipes_sub.add_parser("list")
    activate_recipe = recipes_sub.add_parser("activate")
    activate_recipe.add_argument("recipe_id")
    disable_recipe = recipes_sub.add_parser("disable")
    disable_recipe.add_argument("recipe_id")

    skills = subparsers.add_parser("skills")
    skills.add_argument("--workspace", type=Path, default=Path.cwd())
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_sub.add_parser("list")
    view = skills_sub.add_parser("view")
    view.add_argument("skill_name")
    skills_sub.add_parser("candidates")
    install_bundled = skills_sub.add_parser("install-bundled")
    install_bundled.add_argument("skill_names", nargs="*")
    install_clawhub = skills_sub.add_parser("install-clawhub")
    install_clawhub.add_argument("skill_spec")
    install_hermes = skills_sub.add_parser("install-hermes")
    install_hermes.add_argument("skill_spec")
    view_candidate = skills_sub.add_parser("view-candidate")
    view_candidate.add_argument("candidate_id")
    approve = skills_sub.add_parser("approve")
    approve.add_argument("candidate_id")
    reject = skills_sub.add_parser("reject")
    reject.add_argument("candidate_id")
    disable_skill = skills_sub.add_parser("disable")
    disable_skill.add_argument("skill_name")

    tools = subparsers.add_parser("tools")
    tools_sub = tools.add_subparsers(dest="tools_command", required=True)
    tools_sub.add_parser("list")
    tools_sub.add_parser("status")
    enable_toolset = tools_sub.add_parser("enable")
    enable_toolset.add_argument("toolset")
    disable_toolset = tools_sub.add_parser("disable")
    disable_toolset.add_argument("toolset")

    return parser


def _prompt_with_default(label: str, default: str) -> str:
    value = _sanitize_pasted_input(input(f"{label} [{default}]: ")).strip()
    return value or default


def _sanitize_pasted_input(value: str) -> str:
    return _BRACKETED_PASTE_PATTERN.sub("", value) if value else value


def _prompt_secret(label: str) -> str:
    return _sanitize_pasted_input(getpass.getpass(f"{label}: ")).strip()


def _print_model_setup_header(existing: ModelConfiguration | None) -> None:
    print()
    print("Model Provider Setup")
    if existing:
        print(f"Current: {existing.provider}/{existing.model}")
        print(f"Endpoint: {existing.base_url}")
    else:
        print("Current: not configured")
    print()


def _choose_from_menu(label: str, choices: list[str], default: int = 0) -> str:
    for index, choice in enumerate(choices, start=1):
        suffix = " (default)" if index - 1 == default else ""
        print(f"{index}. {choice}{suffix}")
    selected = _sanitize_pasted_input(
        input(f"{label} [1-{len(choices)} or name, Enter={default + 1}]: ")
    ).strip()
    if not selected:
        return choices[default]
    if selected.isdigit() and 1 <= int(selected) <= len(choices):
        return choices[int(selected) - 1]
    if selected in choices:
        return selected
    raise ProviderConfigurationError("provider must be one of: " + ", ".join(choices))


def _choose_provider(provider: str | None, existing: ModelConfiguration | None = None) -> str:
    if provider:
        return provider
    choices = list(PROVIDER_SPECS)
    default = choices.index(existing.provider) if existing and existing.provider in choices else 0
    return _choose_from_menu("Provider", choices, default)


def _choose_model(models: list[str], default: str = "") -> str:
    available = sorted(set(models))
    if not available:
        raise ModelClientError("model provider returned no available models")
    for index, model in enumerate(available, start=1):
        print(f"{index}. {model}")
    fallback = default if default in available else available[0]
    selected = _prompt_with_default("Model name or number", fallback)
    if selected.isdigit() and 1 <= int(selected) <= len(available):
        selected = available[int(selected) - 1]
    if selected not in available:
        raise ProviderConfigurationError(f"selected model is unavailable: {selected}")
    return selected


def _choose_unlisted_model(default: str) -> str:
    model = _prompt_with_default("Model name", default).strip()
    if not model:
        raise ProviderConfigurationError("configured model must not be empty")
    return model


def setup_model(provider_argument: str | None, config_store: ModelConfigStore) -> int:
    try:
        existing = config_store.load()
    except ProviderConfigurationError:
        existing = None
    _print_model_setup_header(existing)
    provider_id = _choose_provider(provider_argument, existing)
    spec = PROVIDER_SPECS[provider_id]

    default_base_url = (
        existing.base_url if existing and existing.provider == provider_id else spec.default_base_url
    )
    base_url = _prompt_with_default("Base URL", default_base_url).rstrip("/")
    default_model = existing.model if existing and existing.provider == provider_id else spec.default_model
    api_key: str | None = None
    if spec.requires_api_key:
        stored_key = config_store.existing_api_key(provider_id)
        prompt = spec.api_key_label
        if stored_key:
            prompt += " (leave blank to keep existing)"
        entered_key = _prompt_secret(prompt)
        api_key = entered_key or stored_key
        if not api_key:
            raise ProviderConfigurationError(f"{spec.api_key_label} must not be empty")

    if spec.supports_model_listing:
        probe_configuration = ModelConfiguration(
            provider=provider_id,
            model=default_model or "setup-probe",
            base_url=base_url,
            timeout=spec.default_timeout,
        )
        probe = create_provider(probe_configuration, config_store, api_key=api_key)
        model_name = _choose_model(probe.list_models(), default_model)
    else:
        model_name = _choose_unlisted_model(default_model)
    configuration = ModelConfiguration(
        provider=provider_id,
        model=model_name,
        base_url=base_url,
        timeout=spec.default_timeout,
    )
    provider = create_provider(configuration, config_store, api_key=api_key)
    provider.validate()
    config_store.save(configuration, api_key=api_key if spec.requires_api_key else None)
    print(
        json.dumps(
            {
                "configured": True,
                "provider": configuration.provider,
                "model": configuration.model,
                "base_url": configuration.base_url,
                "timeout": configuration.timeout,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def doctor(
    db_path: Path,
    config_store: ModelConfigStore,
    workspace: Path | None = None,
    toolset_store: ToolsetConfigStore | None = None,
) -> int:
    workspace = (workspace or Path.cwd()).resolve()
    database_error = ""
    try:
        AutonomyStore(db_path)
        database_writable = True
    except (OSError, sqlite3.Error) as exc:
        database_writable = False
        database_error = str(exc)
    try:
        env_permissions_secure = config_store.env_permissions_secure()
    except OSError:
        env_permissions_secure = False
    autonomy_home = workspace_autonomy_home(workspace)
    skill_store_path = autonomy_home / "skills"
    candidate_store_path = autonomy_home / "skill-candidates"
    skill_store_writable = _directory_writable(skill_store_path)
    candidate_store_writable = _directory_writable(candidate_store_path)
    toolset_store = toolset_store or ToolsetConfigStore(default_toolset_config_dir())
    try:
        toolset_configuration = toolset_store.load()
        toolset_error = ""
    except ToolsetConfigurationError as exc:
        toolset_configuration = None
        toolset_error = str(exc)
    registry_configuration = toolset_configuration or ToolsetConfiguration()
    registry = build_local_tool_registry(
        workspace,
        registry_configuration,
        require_available=False,
    )
    try:
        tool_statuses = registry.tool_statuses()
        checks = {
            "python_3_13_or_newer": sys.version_info >= (3, 13),
            "database_writable": database_writable,
            "database_error": database_error,
            "skill_store_path": str(skill_store_path),
            "skill_store_writable": skill_store_writable,
            "candidate_store_path": str(candidate_store_path),
            "candidate_store_writable": candidate_store_writable,
            "model_configured": False,
            "configuration_valid": False,
            "configuration_source": "workspace",
            "autonomy_home": str(autonomy_home),
            "provider": "",
            "model": "",
            "endpoint": "",
            "credentials_configured": False,
            "env_permissions_secure": env_permissions_secure,
            "model_endpoint_reachable": False,
            "model_available": False,
            "model_error": "",
            "tool_config_path": str(toolset_store.config_path),
            "tool_config_valid": toolset_configuration is not None,
            "tool_config_error": toolset_error,
            "enabled_toolsets": sorted(toolset_configuration.enabled_toolsets) if toolset_configuration else [],
            "toolsets": toolset_catalog_status(
                toolset_configuration,
                tool_statuses,
            ) if toolset_configuration else [],
            "web_readiness": _web_readiness(toolset_configuration, tool_statuses),
            "tools": sorted(registry.names),
        }
    finally:
        registry.close()
    try:
        configuration = config_store.load()
        checks.update(
            {
                "model_configured": True,
                "configuration_valid": True,
                "provider": configuration.provider,
                "model": configuration.model,
                "endpoint": configuration.base_url,
            }
        )
        spec = PROVIDER_SPECS[configuration.provider]
        if spec.requires_api_key:
            config_store.load_api_key(configuration.provider)
            checks["credentials_configured"] = True
            checks["env_permissions_secure"] = config_store.env_permissions_secure()
            if checks["env_permissions_secure"] is not True:
                raise ProviderConfigurationError(
                    f"model secrets file must have mode 0600: {config_store.env_path}"
                )
        else:
            checks["credentials_configured"] = True
        provider = create_provider(configuration, config_store)
        if spec.supports_model_listing:
            models = provider.list_models()
            checks["model_endpoint_reachable"] = True
            checks["model_available"] = configuration.model in models
            if not checks["model_available"]:
                checks["model_error"] = f"configured model is unavailable: {configuration.model}"
        else:
            provider.validate()
            checks["model_endpoint_reachable"] = True
            checks["model_available"] = True
    except (ProviderConfigurationError, ModelClientError, OSError) as exc:
        checks["model_error"] = str(exc)

    print(json.dumps(checks, indent=2, sort_keys=True))
    required = all(
        checks[name]
        for name in (
            "python_3_13_or_newer",
            "database_writable",
            "tool_config_valid",
            "model_configured",
            "configuration_valid",
            "credentials_configured",
            "model_endpoint_reachable",
            "model_available",
        )
    )
    if checks["provider"] in PROVIDER_SPECS and PROVIDER_SPECS[checks["provider"]].requires_api_key:
        required = required and checks["env_permissions_secure"] is True
    return 0 if required else 1


def _directory_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        marker = path / ".doctor-write-test"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink()
        return True
    except OSError:
        return False


def _web_readiness(
    configuration: ToolsetConfiguration | None,
    tool_statuses: dict[str, dict],
) -> dict[str, str | bool]:
    if configuration is None:
        return {"ready": False, "status": "tool_config_invalid", "reason": "tool configuration is invalid"}
    if "browser" not in configuration.enabled_set:
        return {"ready": False, "status": "disabled", "reason": "browser toolset is disabled"}
    status = tool_statuses.get("browser.navigate", {})
    if status.get("available") is True:
        return {"ready": True, "status": "ready", "reason": ""}
    return {
        "ready": False,
        "status": "unavailable",
        "reason": str(status.get("unavailable_reason") or "browser.navigate is unavailable"),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "chrome-host":
        from . import chrome_host

        return chrome_host.run_chrome_host()
    if args.command == "discord-bot":
        from .discord_bot import run_discord_bot

        workspace = args.workspace.resolve()
        _prepare_workspace_storage(workspace)
        return run_discord_bot(
            workspace=workspace,
            db_path=_db_path_for(workspace, args.db),
            max_steps=args.max_steps,
        )
    workspace = _workspace_for_args(args)
    _prepare_workspace_storage(workspace)
    db_path = _db_path_for(workspace, args.db)
    config_dir = _model_config_dir_for(workspace)
    tool_config_dir = _toolset_config_dir_for(workspace)
    config_store = ModelConfigStore(config_dir)
    toolset_store = ToolsetConfigStore(tool_config_dir)
    if args.command is None or args.command == "tui":
        return _run_tui_session(
            workspace=workspace,
            db_path=db_path,
            max_steps=getattr(args, "max_steps", 12),
            config_dir=config_dir,
            tool_config_dir=tool_config_dir,
        )
    if args.command == "model":
        try:
            return setup_model(args.provider, config_store)
        except (ProviderConfigurationError, ModelClientError, OSError) as exc:
            print(f"model setup error: {exc}", file=sys.stderr)
            return 2

    if args.command == "doctor":
        return doctor(db_path, config_store, workspace, toolset_store=toolset_store)
    store = AutonomyStore(db_path)
    if args.command == "inspect":
        print(json.dumps(store.inspect_run(args.run_id), indent=2, sort_keys=True))
        return 0
    if args.command == "recipes":
        if args.recipes_command == "list":
            print(json.dumps(jsonable(store.list_recipes()), indent=2, sort_keys=True))
        elif args.recipes_command == "activate":
            store.set_recipe_state(args.recipe_id, status=RecipeStatus.ACTIVE, enabled=True)
        elif args.recipes_command == "disable":
            store.set_recipe_state(args.recipe_id, enabled=False)
        return 0
    if args.command == "skills":
        registry = build_local_tool_registry(
            workspace,
            toolset_store.load(),
        )
        library = ProcedureSkillLibrary(workspace, store)
        try:
            if args.skills_command == "list":
                print(
                    json.dumps(
                        jsonable(library.index(registry.names, include_disabled=True)),
                        indent=2,
                        sort_keys=True,
                    )
                )
            elif args.skills_command == "view":
                print(library.view(args.skill_name, registry.names).raw_content)
            elif args.skills_command == "candidates":
                print(json.dumps(library.list_candidates(), indent=2, sort_keys=True))
            elif args.skills_command == "install-bundled":
                installed = library.install_bundled(args.skill_names or None)
                print(json.dumps(jsonable(installed), indent=2, sort_keys=True))
            elif args.skills_command == "install-clawhub":
                installed = library.install_clawhub(args.skill_spec)
                print(json.dumps(jsonable(installed), indent=2, sort_keys=True))
            elif args.skills_command == "install-hermes":
                installed = library.install_hermes(args.skill_spec)
                print(json.dumps(jsonable(installed), indent=2, sort_keys=True))
            elif args.skills_command == "view-candidate":
                print(library.view_candidate(args.candidate_id).raw_content)
            elif args.skills_command == "approve":
                approved = library.approve_candidate(args.candidate_id)
                print(json.dumps(jsonable(approved.summary), indent=2, sort_keys=True))
            elif args.skills_command == "reject":
                print(json.dumps(library.reject_candidate(args.candidate_id), indent=2, sort_keys=True))
            elif args.skills_command == "disable":
                library.disable(args.skill_name, registry.names)
            return 0
        except (KeyError, FileExistsError, ProcedureSkillError) as exc:
            print(f"skill error: {exc}", file=sys.stderr)
            return 2
    if args.command == "tools":
        try:
            if args.tools_command in {"list", "status"}:
                configuration = toolset_store.load()
                registry = build_local_tool_registry(
                    workspace,
                    configuration,
                    require_available=False,
                )
                try:
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
                finally:
                    registry.close()
            elif args.tools_command == "enable":
                print(
                    json.dumps(
                        toolset_store.enable(args.toolset).as_document(),
                        indent=2,
                        sort_keys=True,
                    )
                )
            elif args.tools_command == "disable":
                print(
                    json.dumps(
                        toolset_store.disable(args.toolset).as_document(),
                        indent=2,
                        sort_keys=True,
                    )
                )
            return 0
        except ToolsetConfigurationError as exc:
            print(f"toolset error: {exc}", file=sys.stderr)
            return 2
    try:
        agent_loop = build_agent_loop(
            workspace,
            db_path,
            config_dir=config_dir,
            tool_config_dir=tool_config_dir,
        )
    except (ProviderConfigurationError, ToolsetConfigurationError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    result = agent_loop.run(
        args.goal,
        max_steps=args.max_steps,
        interactive=not args.non_interactive,
        interface="run",
    )
    print(json.dumps(jsonable(result), indent=2, sort_keys=True))
    return 0 if result.termination.value == "achieved" else 2


def _workspace_for_args(args) -> Path:
    if args.command in {"run", "skills", "tui"}:
        return args.workspace.expanduser().resolve()
    return Path.cwd().resolve()


def _run_tui_session(
    *,
    workspace: Path,
    db_path: Path,
    max_steps: int,
    config_dir: Path,
    tool_config_dir: Path,
) -> int:
    from .ui import AutonomyTUI

    shell = SessionShell(
        workspace=workspace,
        db_path=db_path,
        max_steps=max_steps,
        config_dir=config_dir,
        tool_config_dir=tool_config_dir,
    )
    return AutonomyTUI(shell).run()
