import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from autonomy.cli import SessionShell, build_parser, main
from autonomy.model import ModelClientError
from autonomy import (
    ActionRecipe,
    AutonomyStore,
    ConversationDecision,
    ConversationMode,
    ConversationResponse,
    ModelConfigStore,
    ModelConfiguration,
    ProcedureSkillDraft,
    ProcedureSkillLibrary,
    RecipeStatus,
    RunResult,
    TerminationReason,
)
from autonomy.toolsets import ToolsetConfigStore


class FakeProvider:
    def __init__(self, models=None, error=None):
        self.models = models or ["qwen2.5vl:7b"]
        self.error = error

    def list_models(self):
        if self.error:
            raise self.error
        return self.models

    def validate(self):
        if self.error:
            raise self.error


class FakeAgentLoop:
    def __init__(self):
        self.calls = []

    def run(
        self,
        goal,
        max_steps=12,
        interactive=True,
        interface="run",
        conversation_context="",
        journal_metadata=None,
    ):
        self.calls.append(
            {
                "goal": goal,
                "max_steps": max_steps,
                "interactive": interactive,
                "interface": interface,
                "conversation_context": conversation_context,
                "journal_metadata": journal_metadata or {},
            }
        )
        return RunResult(
            run_id=f"run-{len(self.calls)}",
            goal=goal,
            termination=TerminationReason.ACHIEVED,
            steps_executed=max_steps,
            reason=f"handled {goal}",
        )


class TaskRouter:
    def route(self, conversation_context, user_input):
        del conversation_context
        return ConversationDecision(
            mode=ConversationMode.TASK,
            task_goal=user_input,
            reason="test task",
        )


class TaskResponder:
    def respond_to_chat(self, conversation_context, user_input):
        del conversation_context, user_input
        return "chat reply"

    def summarize_task_result(self, conversation_context, user_input, result):
        del conversation_context, user_input
        return f"handled {result.goal}"


class AutonomyNativeCliTest(unittest.TestCase):
    def test_doctor_reports_agent_loop_and_tools(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.tools.browser_tools_available", return_value=(False, "missing browser")),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = main(["--db", str(Path(tmpdir) / "doctor.db"), "doctor"])

        self.assertEqual(result, 1)
        self.assertIn('"python_3_13_or_newer": true', output.getvalue())
        self.assertIn('"model_configured": false', output.getvalue())
        self.assertIn('"filesystem.read"', output.getvalue())
        self.assertIn('"tool_config_valid": true', output.getvalue())
        self.assertIn('"enabled_toolsets"', output.getvalue())

    def test_doctor_reports_database_failure_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            invalid_parent = Path(tmpdir) / "not-a-directory"
            invalid_parent.write_text("file", encoding="utf-8")
            with (
                patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
                patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
                redirect_stdout(io.StringIO()) as output,
            ):
                result = main(["--db", str(invalid_parent / "doctor.db"), "doctor"])

        self.assertEqual(result, 1)
        self.assertIn('"database_writable": false', output.getvalue())
        self.assertIn('"database_error":', output.getvalue())

    def test_run_reports_missing_model_configuration_without_traceback(self):
        environment = {
            "AUTONOMY_MODEL": "",
            "AUTONOMY_API_KEY": "",
            "OPENAI_API_KEY": "",
        }
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict("os.environ", environment, clear=False),
            patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
            redirect_stderr(io.StringIO()) as error,
        ):
            result = main(
                [
                    "--db",
                    str(Path(tmpdir) / "run.db"),
                    "run",
                    "inspect repository",
                    "--non-interactive",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("configuration error:", error.getvalue())
        self.assertIn("autonomy model setup", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())

    def test_parser_allows_no_subcommand_for_chat(self):
        args = build_parser().parse_args([])

        self.assertIsNone(args.command)

    def test_chat_subcommand_enters_session_shell(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.SessionShell.run", return_value=0) as shell_run,
        ):
            result = main(
                [
                    "--db",
                    str(Path(tmpdir) / "chat.db"),
                    "chat",
                    "--workspace",
                    tmpdir,
                    "--max-steps",
                    "4",
                ]
            )

        self.assertEqual(result, 0)
        shell_run.assert_called_once()

    def test_no_subcommand_enters_session_shell(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.SessionShell.run", return_value=0) as shell_run,
        ):
            result = main(["--db", str(Path(tmpdir) / "chat.db")])

        self.assertEqual(result, 0)
        shell_run.assert_called_once()

    def test_session_shell_routes_natural_language_through_conversation_loop(self):
        result_payload = RunResult(
            run_id="run-1",
            goal="inspect repository",
            termination=TerminationReason.ACHIEVED,
            steps_executed=1,
            reason="done",
        )
        response = ConversationResponse(
            session_id="session",
            user_turn_id="user-turn",
            assistant_turn_id="assistant-turn",
            run_result=result_payload,
            reply="run_id: run-1\ntermination: achieved\nsteps: 1\nreason: done",
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch(
                "autonomy.cli.ConversationLoop.handle_user_input",
                return_value=response,
            ) as handle_user_input,
        ):
            output = io.StringIO()
            inputs = iter(["inspect repository", "/exit"])
            shell = SessionShell(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "chat.db",
                max_steps=3,
                config_dir=Path(tmpdir) / "config",
                input_func=lambda prompt: next(inputs),
                output=output,
                agent_loop_factory=lambda workspace, db_path: FakeAgentLoop(),
                router=TaskRouter(),
                responder=TaskResponder(),
            )

            result = shell.run()

        self.assertEqual(result, 0)
        handle_user_input.assert_called_once_with("inspect repository")
        self.assertIn("run_id: run-1", output.getvalue())

    def test_session_shell_runs_goal_and_summarizes_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeAgentLoop()
            output = io.StringIO()
            inputs = iter(["inspect repository", "/exit"])
            shell = SessionShell(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "chat.db",
                max_steps=3,
                config_dir=Path(tmpdir) / "config",
                input_func=lambda prompt: next(inputs),
                output=output,
                agent_loop_factory=lambda workspace, db_path: fake,
                router=TaskRouter(),
                responder=TaskResponder(),
            )

            result = shell.run()

        self.assertEqual(result, 0)
        self.assertEqual(
            fake.calls,
            [
                {
                    "goal": "inspect repository",
                    "max_steps": 3,
                    "interactive": True,
                    "interface": "chat",
                    "conversation_context": "",
                    "journal_metadata": {
                        "conversation_session_id": shell.conversation.session_id,
                        "conversation_turn_id": fake.calls[0]["journal_metadata"][
                            "conversation_turn_id"
                        ],
                    },
                }
            ],
        )
        self.assertIn("termination: achieved", output.getvalue())
        self.assertIn("run_id: run-1", output.getvalue())

    def test_session_shell_workspace_and_max_steps_affect_later_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            fake = FakeAgentLoop()
            output = io.StringIO()
            inputs = iter(
                [
                    f"/workspace {workspace}",
                    "/max-steps 5",
                    "inspect repository",
                    "/quit",
                ]
            )
            shell = SessionShell(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "chat.db",
                max_steps=2,
                config_dir=Path(tmpdir) / "config",
                input_func=lambda prompt: next(inputs),
                output=output,
                agent_loop_factory=lambda selected_workspace, db_path: fake,
                router=TaskRouter(),
                responder=TaskResponder(),
            )

            shell.run()

        self.assertEqual(fake.calls[0]["max_steps"], 5)
        self.assertIn(f"workspace: {workspace.resolve()}", output.getvalue())
        self.assertIn("max_steps: 5", output.getvalue())

    def test_session_shell_doctor_outputs_checks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = io.StringIO()
            inputs = iter(["/doctor", "/exit"])
            shell = SessionShell(
                workspace=Path(tmpdir),
                db_path=Path(tmpdir) / "chat.db",
                max_steps=3,
                config_dir=Path(tmpdir) / "config",
                input_func=lambda prompt: next(inputs),
                output=output,
                agent_loop_factory=lambda workspace, db_path: FakeAgentLoop(),
                router=TaskRouter(),
                responder=TaskResponder(),
            )

            result = shell.run()

        self.assertEqual(result, 0)
        self.assertIn('"python_3_13_or_newer": true', output.getvalue())
        self.assertIn('"model_configured": false', output.getvalue())
        self.assertIn('"tool_config_valid": true', output.getvalue())

    def test_session_shell_inspects_run_and_handles_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "chat.db"
            store = AutonomyStore(db_path)
            store.create_run("run-id", "goal")
            store.record_event("run-id", 0, "run_started", {"goal": "goal"})
            output = io.StringIO()
            inputs = iter(["/help", "/inspect run-id", "/exit"])
            shell = SessionShell(
                workspace=Path(tmpdir),
                db_path=db_path,
                max_steps=3,
                config_dir=Path(tmpdir) / "config",
                input_func=lambda prompt: next(inputs),
                output=output,
                agent_loop_factory=lambda workspace, db_path: FakeAgentLoop(),
                router=TaskRouter(),
                responder=TaskResponder(),
            )

            result = shell.run()

        self.assertEqual(result, 0)
        self.assertIn("Commands:", output.getvalue())
        self.assertIn('"run_id": "run-id"', output.getvalue())
        self.assertIn("bye", output.getvalue())

    def test_session_shell_lists_skills_and_recipes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            skill_dir = workspace / ".autonomy" / "skills" / "cli-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                """---
name: cli-skill
description: CLI Skill.
version: 1.0.0
tags: [cli]
platforms: [macos, linux, windows]
requires_tools: [filesystem.read]
---

# CLI Skill
""",
                encoding="utf-8",
            )
            store = AutonomyStore(workspace / "chat.db")
            store.upsert_recipe(
                ActionRecipe(
                    "recipe",
                    "intent",
                    "condition",
                    {"tool": "filesystem.read", "arguments": {"path": "README.md"}},
                    "effect",
                    "verify",
                )
            )
            output = io.StringIO()
            inputs = iter([
                "/skills",
                "/recipes",
                "/recipes list",
                "/recipes activate recipe",
                "/recipes disable recipe",
                "/exit",
            ])
            shell = SessionShell(
                workspace=workspace,
                db_path=workspace / "chat.db",
                max_steps=3,
                config_dir=workspace / "config",
                input_func=lambda prompt: next(inputs),
                output=output,
                agent_loop_factory=lambda workspace, db_path: FakeAgentLoop(),
                router=TaskRouter(),
                responder=TaskResponder(),
            )

            shell.run()
            recipe = AutonomyStore(workspace / "chat.db").list_recipes()[0]

            self.assertIn('"name": "cli-skill"', output.getvalue())
            self.assertIn('"id": "recipe"', output.getvalue())
            self.assertIn("activated: recipe", output.getvalue())
            self.assertIn("disabled: recipe", output.getvalue())
            self.assertEqual(recipe.status, RecipeStatus.ACTIVE)
            self.assertFalse(recipe.enabled)

    def test_session_shell_prompts_for_candidate_skill_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            db_path = Path(tmpdir) / "chat.db"
            store = AutonomyStore(db_path)
            library = ProcedureSkillLibrary(workspace, store)
            candidate = library.write_candidate(
                ProcedureSkillDraft(
                    name="session-procedure",
                    description="Session procedure",
                    body="# Session\n\nFollow steps.",
                ),
                source_run_id="run-1",
            )
            output = io.StringIO()
            inputs = iter(["inspect repository", "v", "y", "/exit"])
            response = ConversationResponse(
                session_id="session",
                user_turn_id="user",
                assistant_turn_id="assistant",
                run_result=RunResult(
                    "run-1",
                    "inspect repository",
                    TerminationReason.ACHIEVED,
                    2,
                    "done",
                ),
                reply="run_id: run-1\ntermination: achieved\nsteps: 2\nreason: done",
                candidate_skills=(candidate,),
            )
            shell = SessionShell(
                workspace=workspace,
                db_path=db_path,
                max_steps=3,
                config_dir=Path(tmpdir) / "config",
                input_func=lambda prompt: next(inputs),
                output=output,
                agent_loop_factory=lambda workspace, db_path: FakeAgentLoop(),
                router=TaskRouter(),
                responder=TaskResponder(),
            )

            with patch(
                "autonomy.cli.ConversationLoop.handle_user_input",
                return_value=response,
            ):
                result = shell.run()
            approved_exists = (
                workspace
                / ".autonomy"
                / "skills"
                / "session-procedure"
                / "SKILL.md"
            ).is_file()

        self.assertEqual(result, 0)
        self.assertIn("Candidate Skill created:", output.getvalue())
        self.assertIn("# Session", output.getvalue())
        self.assertIn("approved: session-procedure", output.getvalue())
        self.assertTrue(approved_exists)

    def test_session_shell_prompts_for_candidate_recipe_activation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            db_path = Path(tmpdir) / "chat.db"
            store = AutonomyStore(db_path)
            recipe = ActionRecipe(
                "candidate-recipe",
                "inspect repository",
                "Observed in successful outcomes.",
                {
                    "tool": "filesystem.read",
                    "arguments": {"path": "README.md"},
                    "purpose": "read README",
                },
                "read README",
                "evaluate outcome",
                evidence_count=2,
            )
            store.upsert_recipe(recipe)
            output = io.StringIO()
            inputs = iter(["inspect repository", "v", "y", "/exit"])
            response = ConversationResponse(
                session_id="session",
                user_turn_id="user",
                assistant_turn_id="assistant",
                run_result=RunResult(
                    "run-1",
                    "inspect repository",
                    TerminationReason.ACHIEVED,
                    1,
                    "done",
                ),
                reply="run_id: run-1\ntermination: achieved\nsteps: 1\nreason: done",
                action_recipe_candidates=(
                    {
                        "id": recipe.id,
                        "intent": recipe.intent,
                        "preconditions": recipe.preconditions,
                        "action_template": recipe.action_template,
                        "expected_effect": recipe.expected_effect,
                        "verification_plan": recipe.verification_plan,
                        "status": recipe.status.value,
                        "enabled": recipe.enabled,
                        "evidence_count": recipe.evidence_count,
                    },
                ),
            )
            shell = SessionShell(
                workspace=workspace,
                db_path=db_path,
                max_steps=3,
                config_dir=Path(tmpdir) / "config",
                input_func=lambda prompt: next(inputs),
                output=output,
                agent_loop_factory=lambda workspace, db_path: FakeAgentLoop(),
                router=TaskRouter(),
                responder=TaskResponder(),
            )

            with patch(
                "autonomy.cli.ConversationLoop.handle_user_input",
                return_value=response,
            ):
                result = shell.run()
            updated = AutonomyStore(db_path).list_recipes()[0]

        self.assertEqual(result, 0)
        self.assertIn("Candidate ActionRecipe learned:", output.getvalue())
        self.assertIn('"id": "candidate-recipe"', output.getvalue())
        self.assertIn("activated: candidate-recipe", output.getvalue())
        self.assertEqual(updated.status, RecipeStatus.ACTIVE)
        self.assertTrue(updated.enabled)

    def test_session_shell_can_disable_candidate_recipe_from_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            db_path = Path(tmpdir) / "chat.db"
            store = AutonomyStore(db_path)
            recipe = ActionRecipe(
                "candidate-recipe",
                "inspect repository",
                "Observed in successful outcomes.",
                {"tool": "filesystem.read", "arguments": {"path": "README.md"}},
                "read README",
                "evaluate outcome",
                evidence_count=2,
            )
            store.upsert_recipe(recipe)
            output = io.StringIO()
            inputs = iter(["inspect repository", "d", "/exit"])
            response = ConversationResponse(
                session_id="session",
                user_turn_id="user",
                assistant_turn_id="assistant",
                run_result=RunResult(
                    "run-1",
                    "inspect repository",
                    TerminationReason.ACHIEVED,
                    1,
                    "done",
                ),
                reply="run_id: run-1\ntermination: achieved\nsteps: 1\nreason: done",
                action_recipe_candidates=(
                    {
                        "id": recipe.id,
                        "action_template": recipe.action_template,
                        "evidence_count": recipe.evidence_count,
                    },
                ),
            )
            shell = SessionShell(
                workspace=workspace,
                db_path=db_path,
                max_steps=3,
                config_dir=Path(tmpdir) / "config",
                input_func=lambda prompt: next(inputs),
                output=output,
                agent_loop_factory=lambda workspace, db_path: FakeAgentLoop(),
                router=TaskRouter(),
                responder=TaskResponder(),
            )

            with patch(
                "autonomy.cli.ConversationLoop.handle_user_input",
                return_value=response,
            ):
                result = shell.run()
            updated = AutonomyStore(db_path).list_recipes()[0]

        self.assertEqual(result, 0)
        self.assertIn("disabled: candidate-recipe", output.getvalue())
        self.assertFalse(updated.enabled)

    def test_doctor_checks_configured_model_availability(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.create_provider", return_value=FakeProvider()),
            redirect_stdout(io.StringIO()) as output,
        ):
            ModelConfigStore(Path(tmpdir) / "config").save(
                ModelConfiguration(
                    "ollama",
                    "qwen2.5vl:7b",
                    "http://127.0.0.1:11434/v1",
                    180,
                )
            )
            result = main(["--db", str(Path(tmpdir) / "doctor.db"), "doctor"])

        self.assertEqual(result, 0)
        self.assertIn('"provider": "ollama"', output.getvalue())
        self.assertIn('"model_endpoint_reachable": true', output.getvalue())
        self.assertIn('"model_available": true', output.getvalue())

    def test_doctor_reports_configured_endpoint_failure(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
            patch(
                "autonomy.cli.create_provider",
                return_value=FakeProvider(error=ModelClientError("endpoint is unreachable")),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            ModelConfigStore(Path(tmpdir) / "config").save(
                ModelConfiguration(
                    "ollama",
                    "qwen2.5vl:7b",
                    "http://127.0.0.1:11434/v1",
                    180,
                )
            )
            result = main(["--db", str(Path(tmpdir) / "doctor.db"), "doctor"])

        self.assertEqual(result, 1)
        self.assertIn('"model_endpoint_reachable": false', output.getvalue())
        self.assertIn("endpoint is unreachable", output.getvalue())

    def test_doctor_rejects_insecure_openai_secret_permissions(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.create_provider", return_value=FakeProvider(models=["gpt-test"])),
            redirect_stdout(io.StringIO()) as output,
        ):
            store = ModelConfigStore(Path(tmpdir) / "config")
            store.save(
                ModelConfiguration(
                    "openai-api",
                    "gpt-test",
                    "https://api.openai.com/v1",
                    60,
                ),
                openai_api_key="secret-value",
            )
            store.env_path.chmod(0o644)
            result = main(["--db", str(Path(tmpdir) / "doctor.db"), "doctor"])

        self.assertEqual(result, 1)
        self.assertIn('"env_permissions_secure": false', output.getvalue())
        self.assertIn("mode 0600", output.getvalue())
        self.assertNotIn("secret-value", output.getvalue())

    def test_model_setup_configures_and_replaces_ollama(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.create_provider", return_value=FakeProvider()),
            patch("builtins.input", side_effect=["", ""]),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = main(["model", "setup", "ollama"])
            saved = ModelConfigStore(Path(tmpdir) / "config").load()

        self.assertEqual(result, 0)
        self.assertEqual(saved.provider, "ollama")
        self.assertEqual(saved.model, "qwen2.5vl:7b")
        self.assertNotIn("api_key", output.getvalue())

    def test_model_setup_openai_saves_secret_with_secure_permissions(self):
        fake = FakeProvider(models=["gpt-test"])
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_model_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.cli.create_provider", return_value=fake),
            patch("builtins.input", side_effect=["", ""]),
            patch("autonomy.cli.getpass.getpass", return_value="secret-value"),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = main(["model", "setup", "openai-api"])
            store = ModelConfigStore(Path(tmpdir) / "config")
            self.assertEqual(result, 0)
            self.assertEqual(store.load().provider, "openai-api")
            self.assertEqual(store.load_openai_api_key(), "secret-value")
            self.assertTrue(store.env_permissions_secure())
            self.assertNotIn("secret-value", output.getvalue())

    def test_failed_model_setup_does_not_replace_existing_configuration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir) / "config"
            store = ModelConfigStore(config_dir)
            original = ModelConfiguration(
                "ollama",
                "qwen2.5vl:7b",
                "http://127.0.0.1:11434/v1",
                180,
            )
            store.save(original)
            with (
                patch("autonomy.cli.default_model_config_dir", return_value=config_dir),
                patch(
                    "autonomy.cli.create_provider",
                    return_value=FakeProvider(error=ModelClientError("validation failed")),
                ),
                patch("builtins.input", side_effect=["", ""]),
                redirect_stderr(io.StringIO()),
            ):
                result = main(["model", "setup", "ollama"])

            self.assertEqual(result, 2)
            self.assertEqual(store.load(), original)

    def test_canceled_model_commands_and_run_overrides_are_rejected(self):
        parser = build_parser()
        for arguments in (
            ["model", "status"],
            ["model", "list"],
            ["model", "models"],
            ["model", "use"],
            ["model", "test"],
            ["run", "goal", "--provider", "ollama"],
            ["run", "goal", "--model", "qwen2.5vl:7b"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                parser.parse_args(arguments)

    def test_recipes_cli_lists_action_recipes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "recipes.db"
            store = AutonomyStore(db_path)
            store.upsert_recipe(
                ActionRecipe(
                    "recipe",
                    "intent",
                    "condition",
                    {"tool": "filesystem.read", "arguments": {"path": "README.md"}},
                    "effect",
                    "verify",
                )
            )
            with redirect_stdout(io.StringIO()) as output:
                result = main(["--db", str(db_path), "recipes", "list"])

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())[0]["id"], "recipe")

    def test_tools_cli_lists_status_and_persists_enable_disable(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
            patch("autonomy.tools.browser_tools_available", return_value=(False, "missing browser")),
        ):
            with redirect_stdout(io.StringIO()) as output:
                result = main(["tools", "status"])
            status = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertIn("browser", {row["name"] for row in status})
            self.assertIn("web", {row["name"] for row in status})
            file_row = next(row for row in status if row["name"] == "file")
            terminal_row = next(row for row in status if row["name"] == "terminal")
            web_row = next(row for row in status if row["name"] == "web")
            browser_row = next(row for row in status if row["name"] == "browser")
            self.assertTrue(file_row["enabled"])
            self.assertTrue(file_row["implemented"])
            self.assertIn("filesystem.read", file_row["available_tools"])
            self.assertIn("filesystem.tree", file_row["available_tools"])
            self.assertIn("filesystem.outline", file_row["available_tools"])
            self.assertIn("filesystem.imports", file_row["available_tools"])
            self.assertIn("filesystem.symbol_search", file_row["available_tools"])
            self.assertIn("filesystem.syntax_check", file_row["available_tools"])
            self.assertTrue(terminal_row["enabled"])
            self.assertIn("shell.execute", terminal_row["available_tools"])
            self.assertIn("process.start", terminal_row["available_tools"])
            self.assertTrue(web_row["implemented"])
            self.assertFalse(web_row["enabled"])
            self.assertTrue(browser_row["implemented"])
            self.assertFalse(browser_row["enabled"])
            self.assertEqual(browser_row["available_tools"], [])
            self.assertEqual(browser_row["unavailable_tools"][0]["reason"], "missing browser")

            with redirect_stdout(io.StringIO()):
                result = main(["tools", "disable", "file"])
            self.assertEqual(result, 0)
            saved = ToolsetConfigStore(Path(tmpdir) / "config").load()
            self.assertNotIn("file", saved.enabled_toolsets)

            with redirect_stdout(io.StringIO()):
                result = main(["tools", "enable", "browser"])
            self.assertEqual(result, 0)
            saved = ToolsetConfigStore(Path(tmpdir) / "config").load()
            self.assertIn("browser", saved.enabled_toolsets)

            with redirect_stdout(io.StringIO()):
                result = main(["tools", "enable", "web"])
            self.assertEqual(result, 0)
            saved = ToolsetConfigStore(Path(tmpdir) / "config").load()
            self.assertIn("web", saved.enabled_toolsets)

            with redirect_stdout(io.StringIO()) as output:
                result = main(["tools", "status"])
            self.assertEqual(result, 0)
            status = json.loads(output.getvalue())
            web_row = next(row for row in status if row["name"] == "web")
            browser_row = next(row for row in status if row["name"] == "browser")
            self.assertTrue(web_row["enabled"])
            self.assertEqual(web_row["available_tools"], ["web.fetch", "web.extract", "web.links"])
            self.assertTrue(browser_row["enabled"])
            self.assertTrue(browser_row["implemented"])
            self.assertEqual(browser_row["available_tools"], [])

    def test_tools_cli_rejects_unknown_toolset(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("autonomy.cli.default_toolset_config_dir", return_value=Path(tmpdir) / "config"),
            redirect_stderr(io.StringIO()) as error,
        ):
            result = main(["tools", "enable", "not-a-toolset"])

        self.assertEqual(result, 2)
        self.assertIn("unknown toolset", error.getvalue())

    def test_skills_cli_lists_views_and_approves_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            db_path = Path(tmpdir) / "skills.db"
            store = AutonomyStore(db_path)
            library = ProcedureSkillLibrary(workspace, store)
            candidate = library.write_candidate(
                ProcedureSkillDraft(
                    name="cli-procedure",
                    description="CLI procedure",
                    body="# CLI\n\nFollow steps.",
                    requires_tools=("filesystem.read",),
                )
            )
            with redirect_stdout(io.StringIO()) as output:
                result = main(
                    [
                        "--db",
                        str(db_path),
                        "skills",
                        "--workspace",
                        str(workspace),
                        "view-candidate",
                        candidate["candidate_id"],
                    ]
                )
            self.assertEqual(result, 0)
            self.assertIn("# CLI", output.getvalue())

            with redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--db",
                        str(db_path),
                        "skills",
                        "--workspace",
                        str(workspace),
                        "approve",
                        candidate["candidate_id"],
                    ]
                )
            self.assertEqual(result, 0)

            with redirect_stdout(io.StringIO()) as output:
                result = main(
                    [
                        "--db",
                        str(db_path),
                        "skills",
                        "--workspace",
                        str(workspace),
                        "list",
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())[0]["name"], "cli-procedure")

            with redirect_stdout(io.StringIO()) as output:
                result = main(
                    [
                        "--db",
                        str(db_path),
                        "skills",
                        "--workspace",
                        str(workspace),
                        "view",
                        "cli-procedure",
                    ]
            )
            self.assertEqual(result, 0)
            self.assertIn("# CLI", output.getvalue())

            rejected = library.write_candidate(
                ProcedureSkillDraft(
                    name="reject-procedure",
                    description="Reject procedure",
                    body="# Reject\n\nFollow steps.",
                )
            )
            with redirect_stdout(io.StringIO()) as output:
                result = main(
                    [
                        "--db",
                        str(db_path),
                        "skills",
                        "--workspace",
                        str(workspace),
                        "reject",
                        rejected["candidate_id"],
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "rejected")

    def test_skills_cli_installs_bundled_web_browser_skills(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            db_path = Path(tmpdir) / "skills.db"

            with redirect_stdout(io.StringIO()) as output:
                result = main(
                    [
                        "--db",
                        str(db_path),
                        "skills",
                        "--workspace",
                        str(workspace),
                        "install-bundled",
                        "web-research",
                        "website-inspection",
                    ]
                )
            installed = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(
                [summary["name"] for summary in installed],
                ["web-research", "website-inspection"],
            )
            self.assertTrue(
                (
                    workspace
                    / ".autonomy"
                    / "skills"
                    / "web-research"
                    / "SKILL.md"
                ).is_file()
            )

    def test_curator_cli_is_not_exposed(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["curator", "status"])


if __name__ == "__main__":
    unittest.main()
