from __future__ import annotations

import io
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from autonomy.cli import build_parser
from autonomy.models import (
    Action,
    ActionRecipe,
    ConversationDecision,
    ConversationMode,
    ConversationResponse,
    RecipeStatus,
    RiskLevel,
    RunResult,
    TerminationReason,
)
from autonomy.ui import AutonomyTUI


class FakeConversation:
    def __init__(self, response: ConversationResponse, agent_loop_factory=None):
        self.response = response
        self.inputs: list[str] = []
        if agent_loop_factory is not None:
            self.agent_loop_factory = agent_loop_factory

    def handle_user_input(self, text: str) -> ConversationResponse:
        self.inputs.append(text)
        return self.response


class FakeShell:
    def __init__(
        self,
        inputs: list[str],
        response: ConversationResponse,
        *,
        config_dir: Path | None = None,
        tool_config_dir: Path | None = None,
    ):
        self.workspace = Path("/workspace")
        self.db_path = Path("/workspace/.autonomy/autonomy.db")
        self.max_steps = 5
        if config_dir is not None:
            self.config_dir = config_dir
        if tool_config_dir is not None:
            self.tool_config_dir = tool_config_dir
        self.output = io.StringIO()
        self.conversation = FakeConversation(response)
        self.inputs = inputs
        self.commands: list[str] = []
        self.skill_prompts: list[tuple[dict, ...]] = []
        self.recipe_prompts: list[tuple[dict, ...]] = []

    def input_func(self, prompt: str) -> str:
        if not self.inputs:
            raise EOFError
        return self.inputs.pop(0)

    def _handle_command(self, line: str) -> bool:
        self.commands.append(line)
        print(f"command handled: {line}", file=self.output)
        return True

    def _handle_candidate_skill_prompts(self, candidates: tuple[dict, ...]) -> None:
        self.skill_prompts.append(candidates)

    def _handle_candidate_recipe_prompts(self, candidates: tuple[dict, ...]) -> None:
        self.recipe_prompts.append(candidates)


class FakeStore:
    def __init__(self, journal, recipes=()):
        self.journal = journal
        self.recipes = list(recipes)
        self.recipe_states: list[tuple[str, RecipeStatus | None, bool | None]] = []

    def inspect_run(self, run_id: str):
        if run_id not in self.journal:
            raise KeyError(run_id)
        return self.journal[run_id]

    def list_recipes(self):
        return self.recipes

    def set_recipe_state(
        self,
        recipe_id: str,
        *,
        status: RecipeStatus | None = None,
        enabled: bool | None = None,
    ):
        self.recipe_states.append((recipe_id, status, enabled))


class FakeSkillLibrary:
    def __init__(self, workspace, store):
        self.workspace = workspace
        self.store = store
        self.approved: list[str] = []
        self.rejected: list[str] = []

    def view_candidate(self, candidate_id: str):
        return SimpleNamespace(raw_content=f"# Candidate\n\nid: {candidate_id}")

    def approve_candidate(self, candidate_id: str):
        self.approved.append(candidate_id)
        return SimpleNamespace(summary=SimpleNamespace(name="approved-skill"))

    def reject_candidate(self, candidate_id: str):
        self.rejected.append(candidate_id)
        return {"candidate_id": candidate_id, "status": "rejected"}


class AutonomyTUITest(unittest.TestCase):
    def test_tui_parser_subcommand_exists(self):
        args = build_parser().parse_args(["tui", "--workspace", ".", "--max-steps", "3"])

        self.assertEqual(args.command, "tui")
        self.assertEqual(args.max_steps, 3)

    def test_tui_natural_input_calls_conversation_loop(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="Hello. What would you like to work on?",
            decision=ConversationDecision(ConversationMode.CHAT, reason="greeting"),
        )
        shell = FakeShell(["hello", "/exit"], response)
        result = AutonomyTUI(shell, width=128, color=False).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertEqual(shell.conversation.inputs, ["hello"])
        self.assertIn("Autonomy Workbench", output)
        self.assertIn("conversation-first, self-governed AI workspace", output)
        self.assertIn("ConversationLoop -> AgentLoop -> ActionGateway", output)
        self.assertIn("Boundaries:", output)
        self.assertIn("UI never executes tools directly", output)
        self.assertIn("Skills guide candidate generation; ActionGateway governs execution", output)
        self.assertIn("Review queues:", output)
        self.assertIn(
            "Autonomy · model unknown · tools unknown · max 5 · details compact · turn 0 · runs 0 · workspace",
            output,
        )
        self.assertIn(
            "Autonomy · model unknown · tools unknown · max 5 · details compact · turn 1 · runs 0 · workspace",
            output,
        )
        self.assertIn("You #1", output)
        self.assertIn("hello", output)
        self.assertIn("Conversation #1", output)
        self.assertIn("route: chat", output)
        self.assertIn("router reason: greeting", output)
        self.assertIn("Hello. What would you like to work on?", output)
        self.assertIn("Session Closed", output)
        self.assertIn("turns:        1", output)
        self.assertIn("agent runs:   0", output)
        self.assertEqual(shell.commands, [])

    def test_tui_startup_renders_workspace_model_and_tool_status(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="unused",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.yaml").write_text(
                "\n".join(
                    [
                        "version: 1",
                        "model:",
                        "  provider: ollama",
                        "  model: qwen2.5vl:7b",
                        "  base_url: http://127.0.0.1:11434/v1",
                        "  timeout: 180",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "tools.yaml").write_text(
                "\n".join(
                    [
                        "version: 1",
                        "tools:",
                        "  enabled_toolsets:",
                        "    - file",
                        "    - terminal",
                        "  disabled_tools:",
                        "    - shell.execute",
                    ]
                ),
                encoding="utf-8",
            )
            shell = FakeShell(
                ["/exit"],
                response,
                config_dir=root,
                tool_config_dir=root,
            )
            result = AutonomyTUI(shell, width=140, color=False).run()
            output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertIn("Autonomy Workbench", output)
        self.assertIn("model:    ollama/qwen2.5vl:7b", output)
        self.assertIn("toolsets: file, terminal; 1 disabled tool(s)", output)
        self.assertIn(
            "Autonomy · ollama/qwen2.5vl:7b · tools file,terminal · max 5 · details compact · turn 0 · runs 0 · workspace",
            output,
        )

    def test_tui_startup_uses_responsive_compact_banner(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="unused",
        )
        shell = FakeShell(["/exit"], response)
        result = AutonomyTUI(shell, width=70, color=False).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertIn("AUTONOMY", output)
        self.assertIn("conversation-first, self-governed AI workspace", output)
        self.assertIn("Session:", output)
        self.assertIn("Boundaries:", output)

    def test_tui_task_response_renders_run_metadata_and_review_queue(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=RunResult(
                run_id="run-1",
                goal="inspect project",
                termination=TerminationReason.ACHIEVED,
                steps_executed=2,
                reason="architecture summarized",
            ),
            reply="The project uses ConversationLoop, AgentLoop, and ActionGateway.",
            decision=ConversationDecision(
                ConversationMode.TASK,
                task_goal="inspect project",
                reason="requires project analysis",
            ),
            candidate_skills=({"candidate_id": "skill-1"},),
            action_recipe_candidates=({"id": "recipe-1"},),
        )
        recipe = ActionRecipe(
            id="recipe-1",
            intent="read overview",
            preconditions="",
            action_template={
                "tool": "filesystem.read",
                "arguments": {"path": "README.md"},
                "purpose": "read overview",
            },
            expected_effect="overview read",
            verification_plan="outcome evaluation",
        )
        fake_store = FakeStore({}, recipes=(recipe,))
        shell = FakeShell(["inspect architecture", "", "", "/exit"], response)
        fake_library = FakeSkillLibrary(shell.workspace, fake_store)
        result = AutonomyTUI(
            shell,
            width=140,
            color=False,
            store_factory=lambda _path: fake_store,
            skill_library_factory=lambda workspace, store: fake_library,
        ).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertIn("Task Result #1", output)
        self.assertIn("route: task", output)
        self.assertIn("task goal: inspect project", output)
        self.assertIn("router reason: requires project analysis", output)
        self.assertIn("status:      completed", output)
        self.assertIn("run_id:      run-1", output)
        self.assertIn("termination: achieved", output)
        self.assertIn("next:        /inspect run-1", output)
        self.assertIn("runs 1", output)
        self.assertIn("last run-1 achieved", output)
        self.assertIn("last run:     run-1 achieved", output)
        self.assertIn("agent runs:   1", output)
        self.assertIn("review queue: 1 ProcedureSkill candidate(s), 1 ActionRecipe candidate(s)", output)
        self.assertIn("Skill Review", output)
        self.assertIn("ActionRecipe Review", output)
        self.assertEqual(shell.skill_prompts, [])
        self.assertEqual(shell.recipe_prompts, [])

    def test_tui_skill_review_can_view_and_approve_candidate(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="Review ready.",
            candidate_skills=({"candidate_id": "skill-1", "name": "new-skill"},),
        )
        shell = FakeShell(["hello", "v", "a", "/exit"], response)
        fake_store = FakeStore({})
        fake_library = FakeSkillLibrary(shell.workspace, fake_store)
        result = AutonomyTUI(
            shell,
            width=80,
            color=False,
            store_factory=lambda _path: fake_store,
            skill_library_factory=lambda workspace, store: fake_library,
        ).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertIn("Skill Review", output)
        self.assertIn("Skill Draft", output)
        self.assertIn("approved: approved-skill", output)
        self.assertEqual(fake_library.approved, ["skill-1"])

    def test_tui_action_recipe_review_can_view_and_activate_candidate(self):
        recipe = ActionRecipe(
            id="recipe-1",
            intent="read overview",
            preconditions="",
            action_template={
                "tool": "filesystem.read",
                "arguments": {"path": "README.md"},
                "purpose": "read overview",
            },
            expected_effect="overview read",
            verification_plan="outcome evaluation",
        )
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="Recipe ready.",
            action_recipe_candidates=(
                {
                    "id": "recipe-1",
                    "intent": "read overview",
                    "action_template": recipe.action_template,
                    "evidence_count": 2,
                },
            ),
        )
        shell = FakeShell(["hello", "v", "a", "/exit"], response)
        fake_store = FakeStore({}, recipes=(recipe,))
        result = AutonomyTUI(
            shell,
            width=80,
            color=False,
            store_factory=lambda _path: fake_store,
        ).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertIn("ActionRecipe Review", output)
        self.assertIn("ActionRecipe", output)
        self.assertIn("activated: recipe-1", output)
        self.assertEqual(fake_store.recipe_states, [("recipe-1", RecipeStatus.ACTIVE, True)])

    def test_tui_installs_approval_panel_on_agent_loop_factory(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="unused",
        )
        fake_agent_loop = SimpleNamespace(
            action_gateway=SimpleNamespace(approval=None)
        )

        shell = FakeShell(["a"], response)
        shell.conversation.agent_loop_factory = lambda _workspace, _db_path: fake_agent_loop

        AutonomyTUI(shell, width=80, color=False)
        created = shell.conversation.agent_loop_factory(shell.workspace, shell.db_path)
        action = Action(
            tool="filesystem.write",
            arguments={"path": "README.md"},
            expected_effect="write file",
            verification_plan="outcome evaluation",
            purpose="update docs",
            risk_level=RiskLevel.MEDIUM,
        )
        allowed, reason = created.action_gateway.approval.authorize(action, interactive=True)
        output = shell.output.getvalue()

        self.assertTrue(allowed)
        self.assertEqual(reason, "approved by user")
        self.assertIn("Approval Required", output)
        self.assertIn("filesystem.write", output)
        self.assertIn("approved", output)

    def test_tui_task_path_renders_approval_prompt_when_action_requires_it(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="unused",
        )
        fake_agent_loop = SimpleNamespace(
            action_gateway=SimpleNamespace(approval=None)
        )

        class ApprovalConversation:
            def __init__(self):
                self.inputs: list[str] = []
                self.agent_loop_factory = lambda _workspace, _db_path: fake_agent_loop

            def handle_user_input(self, text: str) -> ConversationResponse:
                self.inputs.append(text)
                created = self.agent_loop_factory(Path("/workspace"), Path("/workspace/.autonomy/autonomy.db"))
                action = Action(
                    tool="filesystem.write",
                    arguments={"path": "README.md"},
                    expected_effect="write file",
                    verification_plan="outcome evaluation",
                    purpose="update docs",
                    risk_level=RiskLevel.MEDIUM,
                )
                allowed, reason = created.action_gateway.approval.authorize(action, interactive=True)
                return ConversationResponse(
                    session_id="s1",
                    user_turn_id="u1",
                    assistant_turn_id="a1",
                    run_result=None,
                    reply=f"approval allowed={allowed}; reason={reason}",
                )

        shell = FakeShell(["write README", "a", "/exit"], response)
        conversation = ApprovalConversation()
        shell.conversation = conversation
        shell.agent_loop_factory = conversation.agent_loop_factory

        result = AutonomyTUI(shell, width=100, color=False).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertEqual(conversation.inputs, ["write README"])
        self.assertIn("Approval Required", output)
        self.assertIn("filesystem.write", output)
        self.assertNotIn("[y/N]", output)
        self.assertIn("[a] approve  [d] deny  [enter] deny", output)
        self.assertIn("approved", output)
        self.assertIn("approval allowed=True; reason=approved by user", output)

    def test_tui_approval_panel_denies_on_enter(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="unused",
        )
        fake_agent_loop = SimpleNamespace(
            action_gateway=SimpleNamespace(approval=None)
        )

        shell = FakeShell([""], response)
        shell.conversation.agent_loop_factory = lambda _workspace, _db_path: fake_agent_loop

        AutonomyTUI(shell, width=80, color=False)
        created = shell.conversation.agent_loop_factory(shell.workspace, shell.db_path)
        action = Action(
            tool="filesystem.write",
            arguments={"path": "README.md"},
            expected_effect="write file",
            verification_plan="outcome evaluation",
            purpose="update docs",
            risk_level=RiskLevel.MEDIUM,
        )
        allowed, reason = created.action_gateway.approval.authorize(action, interactive=True)
        output = shell.output.getvalue()

        self.assertFalse(allowed)
        self.assertEqual(reason, "approval denied by user")
        self.assertIn("Approval Required", output)
        self.assertIn("denied", output)

    def test_tui_task_response_renders_run_journal_timeline(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=RunResult(
                run_id="run-1",
                goal="inspect project",
                termination=TerminationReason.ACHIEVED,
                steps_executed=1,
                reason="done",
            ),
            reply="Done.",
        )
        journal = {
            "run-1": {
                "run": {"run_id": "run-1"},
                "events": [
                    {
                        "step": 0,
                        "event_type": "run_started",
                        "payload": {
                            "interface": "tui",
                            "model_provider": "ollama",
                            "model": "qwen2.5vl:7b",
                        },
                    },
                    {
                        "step": 1,
                        "event_type": "skills_selected",
                        "payload": ["systematic-debugging"],
                    },
                    {
                        "step": 1,
                        "event_type": "candidates_ranked",
                        "payload": [
                            {
                                "actions": [
                                    {
                                        "tool": "filesystem.read",
                                        "arguments": {"path": "README.md"},
                                    }
                                ]
                            }
                        ],
                    },
                    {
                        "step": 1,
                        "event_type": "execution_candidates_blocked",
                        "payload": [
                            {
                                "candidate_id": "candidate-0",
                                "source": "model",
                                "reason": "tool is unavailable: unknown.tool",
                            }
                        ],
                    },
                    {
                        "step": 1,
                        "event_type": "action_selected",
                        "payload": {
                            "tool": "shell.execute",
                            "risk_level": "low",
                            "effective_risk_level": "high",
                            "purpose": "fetch page",
                        },
                    },
                    {
                        "step": 1,
                        "event_type": "approval_decision",
                        "payload": {
                            "allowed": True,
                            "reason": "low risk action",
                        },
                    },
                    {
                        "step": 1,
                        "event_type": "observation",
                        "payload": {
                            "succeeded": True,
                            "evidence": ["README.md read"],
                        },
                    },
                    {
                        "step": 1,
                        "event_type": "outcome_evaluated",
                        "payload": {
                            "goal_status": "achieved",
                            "confidence": 1.0,
                            "reason": "README was summarized",
                        },
                    },
                ],
            }
        }
        shell = FakeShell(["/details full", "inspect architecture", "/exit"], response)
        result = AutonomyTUI(
            shell,
            width=180,
            color=False,
            store_factory=lambda _path: FakeStore(journal),
        ).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertIn("Run dashboard:", output)
        self.assertIn("skills: systematic-debugging", output)
        self.assertIn("candidates: 1 ranked; 1 blocked by boundary", output)
        self.assertIn("selected action: shell.execute [high]", output)
        self.assertIn("approval: allowed", output)
        self.assertIn("observation: succeeded", output)
        self.assertIn("outcome: achieved (1.0)", output)
        self.assertIn("Action trail:", output)
        self.assertIn(
            "step 1: 1 candidate(s) blocked -> shell.execute [high] · fetch page -> approval allowed -> observation succeeded -> outcome achieved (1.0)",
            output,
        )
        self.assertIn("Run timeline:", output)
        self.assertIn("step 0: run started via tui", output)
        self.assertIn("candidates ranked: 1 · top: filesystem.read", output)
        self.assertIn("action selected: shell.execute [high] · fetch page", output)
        self.assertIn("outcome: achieved (1.0) · README was summarized", output)

    def test_tui_compact_details_hides_run_timeline_by_default(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=RunResult(
                run_id="run-compact",
                goal="inspect project",
                termination=TerminationReason.ACHIEVED,
                steps_executed=1,
                reason="done",
            ),
            reply="Done.",
        )
        journal = {
            "run-compact": {
                "run": {"run_id": "run-compact"},
                "events": [
                    {
                        "step": 0,
                        "event_type": "run_started",
                        "payload": {"interface": "tui"},
                    },
                    {
                        "step": 1,
                        "event_type": "action_selected",
                        "payload": {
                            "tool": "filesystem.read",
                            "risk_level": "low",
                            "purpose": "read project overview",
                        },
                    },
                    {
                        "step": 1,
                        "event_type": "outcome_evaluated",
                        "payload": {
                            "goal_status": "achieved",
                            "confidence": 1.0,
                            "reason": "README was summarized",
                        },
                    },
                ],
            }
        }
        shell = FakeShell(["inspect architecture", "/exit"], response)
        result = AutonomyTUI(
            shell,
            width=100,
            color=False,
            store_factory=lambda _path: FakeStore(journal),
        ).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertIn("Action trail:", output)
        self.assertIn("details: compact; use /details full to show the event timeline", output)
        self.assertNotIn("Run timeline:", output)

    def test_tui_ignores_missing_journal_without_hiding_response(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=RunResult(
                run_id="missing-run",
                goal="inspect project",
                termination=TerminationReason.ACHIEVED,
                steps_executed=1,
                reason="done",
            ),
            reply="Still show this.",
        )
        shell = FakeShell(["inspect architecture", "/exit"], response)
        result = AutonomyTUI(
            shell,
            width=80,
            color=False,
            store_factory=lambda _path: FakeStore({}),
        ).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertIn("Still show this.", output)
        self.assertNotIn("Run timeline:", output)

    def test_tui_task_response_labels_attention_and_stopped_states(self):
        blocked = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=RunResult(
                run_id="blocked-run",
                goal="inspect project",
                termination=TerminationReason.BLOCKED,
                steps_executed=1,
                reason="tool failed",
            ),
            reply="I could not complete this.",
        )
        failed = ConversationResponse(
            session_id="s1",
            user_turn_id="u2",
            assistant_turn_id="a2",
            run_result=RunResult(
                run_id="failed-run",
                goal="inspect project",
                termination=TerminationReason.FAILED,
                steps_executed=1,
                reason="unexpected error",
            ),
            reply="The run failed.",
        )

        blocked_shell = FakeShell(["inspect architecture", "/exit"], blocked)
        failed_shell = FakeShell(["inspect architecture", "/exit"], failed)
        blocked_result = AutonomyTUI(
            blocked_shell,
            width=82,
            color=False,
            store_factory=lambda _path: FakeStore({}),
        ).run()
        failed_result = AutonomyTUI(
            failed_shell,
            width=82,
            color=False,
            store_factory=lambda _path: FakeStore({}),
        ).run()

        self.assertEqual(blocked_result, 0)
        self.assertEqual(failed_result, 0)
        self.assertIn("status:      needs attention", blocked_shell.output.getvalue())
        self.assertIn("next:        /inspect blocked-run", blocked_shell.output.getvalue())
        self.assertIn("status:      stopped", failed_shell.output.getvalue())
        self.assertIn("next:        /inspect failed-run", failed_shell.output.getvalue())

    def test_tui_slash_command_delegates_to_shell_handler(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="unused",
        )
        shell = FakeShell(["/doctor", "/exit"], response)
        result = AutonomyTUI(shell, width=72, color=False).run()

        self.assertEqual(result, 0)
        self.assertEqual(shell.commands, ["/doctor"])
        self.assertEqual(shell.conversation.inputs, [])
        self.assertIn("command handled: /doctor", shell.output.getvalue())

    def test_tui_slash_palette_renders_without_delegating(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="unused",
        )
        shell = FakeShell(["/", "/?", "/exit"], response)
        result = AutonomyTUI(shell, width=86, color=False).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertEqual(shell.commands, [])
        self.assertEqual(shell.conversation.inputs, [])
        self.assertEqual(output.count("Command Palette"), 2)
        self.assertIn("/inspect RUN_ID", output)
        self.assertIn("/workspace PATH", output)
        self.assertIn("/recipes", output)

    def test_tui_details_command_is_local_to_tui(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="unused",
        )
        shell = FakeShell(["/details", "/details full", "/details compact", "/details verbose", "/exit"], response)
        result = AutonomyTUI(shell, width=128, color=False).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertEqual(shell.commands, [])
        self.assertEqual(shell.conversation.inputs, [])
        self.assertIn("current mode: compact", output)
        self.assertIn("details mode: full", output)
        self.assertIn("details mode: compact", output)
        self.assertIn("details full · turn 0 · runs 0", output)
        self.assertIn("details compact · turn 0 · runs 0", output)
        self.assertIn("usage: /details compact", output)
        self.assertIn("Session Closed", output)
        self.assertIn("details mode: compact", output)

    def test_tui_eof_renders_session_summary(self):
        response = ConversationResponse(
            session_id="s1",
            user_turn_id="u1",
            assistant_turn_id="a1",
            run_result=None,
            reply="unused",
        )
        shell = FakeShell([], response)
        result = AutonomyTUI(shell, width=84, color=False).run()
        output = shell.output.getvalue()

        self.assertEqual(result, 0)
        self.assertIn("Session Closed", output)
        self.assertIn("turns:        0", output)
        self.assertIn("agent runs:   0", output)
        self.assertIn("details mode: compact", output)
        self.assertIn("last run:     none", output)
        self.assertIn("workspace:    /workspace", output)


if __name__ == "__main__":
    unittest.main()
