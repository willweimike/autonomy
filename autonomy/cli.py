from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Callable, TextIO

from .conversation import ConversationLoop
from .model import AutonomyModel, ModelClientError
from .models import RecipeStatus, jsonable
from .procedure_skills import ProcedureSkillError, ProcedureSkillLibrary
from .providers import (
    PROVIDER_SPECS,
    ModelConfiguration,
    ModelConfigStore,
    ProviderConfigurationError,
    create_provider,
)
from .recipes import RecipeEngine
from .runtime import AutonomyRuntime
from .selection import CandidateSelector
from .store import AutonomyStore
from .tools import ApprovalPolicy, build_local_tool_registry
from .verification import ModelAssistedVerifier


def default_db_path() -> Path:
    return Path(os.environ.get("AUTONOMY_DB", "~/.autonomy/autonomy.db")).expanduser()


def default_model_config_dir() -> Path:
    return Path.home() / ".autonomy"


def build_runtime(
    workspace: Path,
    db_path: Path,
    *,
    config_dir: Path | None = None,
) -> AutonomyRuntime:
    store = AutonomyStore(db_path)
    config_store = ModelConfigStore(config_dir or default_model_config_dir())
    provider = create_provider(config_store.load(), config_store)
    model = AutonomyModel.from_provider(provider)
    procedure_skills = ProcedureSkillLibrary(workspace, store)
    return AutonomyRuntime(
        model=model,
        tools=build_local_tool_registry(workspace),
        verifier=ModelAssistedVerifier(model),
        store=store,
        selector=CandidateSelector(beam_width=3),
        approval=ApprovalPolicy(),
        recipes=RecipeEngine(store),
        procedure_skills=procedure_skills,
    )


class SessionShell:
    PROMPT = "autonomy> "

    def __init__(
        self,
        *,
        workspace: Path,
        db_path: Path,
        max_steps: int,
        config_dir: Path | None = None,
        input_func: Callable[[str], str] = input,
        output: TextIO = sys.stdout,
        runtime_factory: Callable[[Path, Path], AutonomyRuntime] | None = None,
    ):
        self.workspace = workspace.resolve()
        self.db_path = db_path
        self.max_steps = max_steps
        self.config_dir = config_dir or default_model_config_dir()
        self.input_func = input_func
        self.output = output
        self.runtime_factory = runtime_factory or self._build_runtime
        self.conversation = ConversationLoop(
            workspace=self.workspace,
            db_path=self.db_path,
            max_steps=self.max_steps,
            runtime_factory=self.runtime_factory,
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

    def _build_runtime(self, workspace: Path, db_path: Path) -> AutonomyRuntime:
        return build_runtime(workspace, db_path, config_dir=self.config_dir)

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
                        "  /recipes",
                    ]
                )
            )
            return True
        if command == "/doctor":
            with redirect_stdout(self.output):
                doctor(self.db_path, ModelConfigStore(self.config_dir), self.workspace)
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
            self._write(
                json.dumps(
                    jsonable(AutonomyStore(self.db_path).list_recipes()),
                    indent=2,
                    sort_keys=True,
                )
            )
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

    def _handle_skills_command(self, arguments: list[str]) -> None:
        registry = build_local_tool_registry(self.workspace)
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
            elif arguments[0] == "view-candidate" and len(arguments) == 2:
                self._write(library.view_candidate(arguments[1]).raw_content)
            elif arguments[0] == "approve" and len(arguments) == 2:
                approved = library.approve_candidate(arguments[1])
                self._write(json.dumps(jsonable(approved.summary), indent=2, sort_keys=True))
            elif arguments[0] == "reject" and len(arguments) == 2:
                self._write(json.dumps(library.reject_candidate(arguments[1]), indent=2, sort_keys=True))
            else:
                self._write(
                    "usage: /skills [candidates|view-candidate CANDIDATE_ID|approve CANDIDATE_ID|reject CANDIDATE_ID]"
                )
        except (KeyError, FileExistsError, ProcedureSkillError) as exc:
            self._write(f"skill error: {exc}")

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

    def _write(self, message: str) -> None:
        print(message, file=self.output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autonomy",
        description="Start an interactive Autonomy session when no subcommand is provided.",
    )
    parser.add_argument("--db", type=Path, default=default_db_path())
    subparsers = parser.add_subparsers(dest="command", required=False)

    run = subparsers.add_parser("run")
    run.add_argument("goal")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--max-steps", type=int, default=12)
    run.add_argument("--non-interactive", action="store_true")

    chat = subparsers.add_parser("chat")
    chat.add_argument("--workspace", type=Path, default=Path.cwd())
    chat.add_argument("--max-steps", type=int, default=12)

    model = subparsers.add_parser("model")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    setup = model_sub.add_parser("setup")
    setup.add_argument("provider", nargs="?", choices=sorted(PROVIDER_SPECS))

    subparsers.add_parser("doctor")
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("run_id")

    recipes = subparsers.add_parser("recipes")
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
    view_candidate = skills_sub.add_parser("view-candidate")
    view_candidate.add_argument("candidate_id")
    approve = skills_sub.add_parser("approve")
    approve.add_argument("candidate_id")
    reject = skills_sub.add_parser("reject")
    reject.add_argument("candidate_id")
    disable_skill = skills_sub.add_parser("disable")
    disable_skill.add_argument("skill_name")
    return parser


def _prompt_with_default(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _choose_provider(provider: str | None) -> str:
    if provider:
        return provider
    selected = input("Provider [ollama/openai-api]: ").strip()
    if selected not in PROVIDER_SPECS:
        raise ProviderConfigurationError(
            "provider must be one of: " + ", ".join(sorted(PROVIDER_SPECS))
        )
    return selected


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


def setup_model(provider_argument: str | None, config_store: ModelConfigStore) -> int:
    provider_id = _choose_provider(provider_argument)
    spec = PROVIDER_SPECS[provider_id]
    try:
        existing = config_store.load()
    except ProviderConfigurationError:
        existing = None

    default_base_url = (
        existing.base_url if existing and existing.provider == provider_id else spec.default_base_url
    )
    base_url = _prompt_with_default("Base URL", default_base_url).rstrip("/")
    default_model = existing.model if existing and existing.provider == provider_id else ""
    api_key: str | None = None
    if spec.requires_api_key:
        stored_key = config_store.existing_openai_api_key()
        prompt = "OpenAI API key"
        if stored_key:
            prompt += " (leave blank to keep existing)"
        entered_key = getpass.getpass(f"{prompt}: ").strip()
        api_key = entered_key or stored_key
        if not api_key:
            raise ProviderConfigurationError("OpenAI API key must not be empty")

    probe_configuration = ModelConfiguration(
        provider=provider_id,
        model=default_model or "setup-probe",
        base_url=base_url,
        timeout=spec.default_timeout,
    )
    probe = create_provider(probe_configuration, config_store, openai_api_key=api_key)
    model_name = _choose_model(probe.list_models(), default_model)
    configuration = ModelConfiguration(
        provider=provider_id,
        model=model_name,
        base_url=base_url,
        timeout=spec.default_timeout,
    )
    provider = create_provider(configuration, config_store, openai_api_key=api_key)
    provider.validate()
    config_store.save(configuration, openai_api_key=api_key if spec.requires_api_key else None)
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


def doctor(db_path: Path, config_store: ModelConfigStore, workspace: Path | None = None) -> int:
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
    autonomy_home = Path.home() / ".autonomy"
    skill_store_path = autonomy_home / "skills"
    candidate_store_path = autonomy_home / "skill-candidates"
    skill_store_writable = _directory_writable(skill_store_path)
    candidate_store_writable = _directory_writable(candidate_store_path)
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
        "configuration_source": "global",
        "provider": "",
        "model": "",
        "endpoint": "",
        "credentials_configured": False,
        "env_permissions_secure": env_permissions_secure,
        "model_endpoint_reachable": False,
        "model_available": False,
        "model_error": "",
        "tools": sorted(build_local_tool_registry(workspace or Path.cwd()).names),
    }
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
        if configuration.provider == "openai-api":
            config_store.load_openai_api_key()
            checks["credentials_configured"] = True
            checks["env_permissions_secure"] = config_store.env_permissions_secure()
            if checks["env_permissions_secure"] is not True:
                raise ProviderConfigurationError(
                    f"model secrets file must have mode 0600: {config_store.env_path}"
                )
        else:
            checks["credentials_configured"] = True
        provider = create_provider(configuration, config_store)
        models = provider.list_models()
        checks["model_endpoint_reachable"] = True
        checks["model_available"] = configuration.model in models
        if not checks["model_available"]:
            checks["model_error"] = f"configured model is unavailable: {configuration.model}"
    except (ProviderConfigurationError, ModelClientError, OSError) as exc:
        checks["model_error"] = str(exc)

    print(json.dumps(checks, indent=2, sort_keys=True))
    required = all(
        checks[name]
        for name in (
            "python_3_13_or_newer",
            "database_writable",
            "model_configured",
            "configuration_valid",
            "credentials_configured",
            "model_endpoint_reachable",
            "model_available",
        )
    )
    if checks["provider"] == "openai-api":
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_store = ModelConfigStore(default_model_config_dir())
    if args.command is None:
        return SessionShell(
            workspace=Path.cwd(),
            db_path=args.db,
            max_steps=12,
            config_dir=default_model_config_dir(),
        ).run()
    if args.command == "chat":
        return SessionShell(
            workspace=args.workspace,
            db_path=args.db,
            max_steps=args.max_steps,
            config_dir=default_model_config_dir(),
        ).run()
    if args.command == "model":
        try:
            return setup_model(args.provider, config_store)
        except (ProviderConfigurationError, ModelClientError, OSError) as exc:
            print(f"model setup error: {exc}", file=sys.stderr)
            return 2

    if args.command == "doctor":
        return doctor(args.db, config_store)
    store = AutonomyStore(args.db)
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
        registry = build_local_tool_registry(args.workspace.resolve())
        library = ProcedureSkillLibrary(args.workspace.resolve(), store)
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
    try:
        runtime = build_runtime(
            args.workspace.resolve(),
            args.db,
            config_dir=default_model_config_dir(),
        )
    except (ProviderConfigurationError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    result = runtime.run(
        args.goal,
        max_steps=args.max_steps,
        interactive=not args.non_interactive,
        interface="run",
    )
    print(json.dumps(jsonable(result), indent=2, sort_keys=True))
    return 0 if result.termination.value == "achieved" else 2
