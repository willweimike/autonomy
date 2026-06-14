import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autonomy
from autonomy import (
    Action,
    ActionGateway,
    ActionIntent,
    AgentLoop,
    ApprovalPolicy,
    AutonomyStore,
    CandidatePath,
    CandidateSelector,
    DeterministicOutcomeEvaluator,
    GoalStatus,
    Observation,
    Outcome,
    ProcedureSkillDraft,
    ProcedureSkillLibrary,
    RiskLevel,
    TerminationReason,
    ToolRegistry,
    ToolsetConfiguration,
    build_local_tool_registry,
)
from autonomy.outcome import ModelAssistedOutcomeEvaluator


class SequenceModel:
    def __init__(self, candidates_by_call):
        self.candidates_by_call = list(candidates_by_call)
        self.calls = 0

    def select_procedure_skills(self, state, skill_index, available_tools):
        del state, available_tools
        return [skill.name for skill in skill_index[:3]]

    def propose(self, state, available_tools, procedure_skills, tool_specs=None):
        del state, available_tools, procedure_skills, tool_specs
        index = min(self.calls, len(self.candidates_by_call) - 1)
        self.calls += 1
        return self.candidates_by_call[index]

    def draft_procedure_skill(self, state):
        del state
        return ProcedureSkillDraft(
            name="learned-procedure",
            description="A learned procedure.",
            body="# Learned\n\nFollow the successful steps.",
            requires_tools=("test.tool",),
        )


def candidate(
    *,
    goal_achieving=False,
    tool="test.tool",
    arguments=None,
    nonce=None,
    purpose="produce evidence",
):
    return CandidatePath(
        source="test",
        actions=[
            ActionIntent(
                tool=tool,
                arguments=(
                    arguments
                    if arguments is not None
                    else {"_goal_achieving": goal_achieving, "_nonce": nonce}
                ),
                purpose=purpose,
            )
        ],
    )


class AutonomyNativeAgentLoopTest(unittest.TestCase):
    def test_agent_loop_replaces_runtime_public_api(self):
        self.assertIn("AgentLoop", autonomy.__all__)
        self.assertIn("ActionGateway", autonomy.__all__)
        self.assertNotIn("AutonomyRuntime", autonomy.__all__)
        self.assertNotIn("AutonomyKernel", autonomy.__all__)
        self.assertFalse(hasattr(autonomy, "AutonomyRuntime"))
        self.assertFalse(hasattr(autonomy, "AutonomyKernel"))

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = AutonomyStore(Path(self.tmpdir.name) / "autonomy.db")
        self.registry = ToolRegistry()
        self.executions = []

        def execute(arguments):
            self.executions.append(arguments)
            return Observation("", True, output="ok", evidence=("verified:test",))

        self.registry.register("test.tool", execute)

    def tearDown(self):
        self.tmpdir.cleanup()

    def agent_loop(
        self,
        model,
        approval=None,
        outcome_evaluator=None,
        procedure_skills=None,
        curator_daemon=None,
    ):
        return AgentLoop(
            model=model,
            action_gateway=ActionGateway(
                tools=self.registry,
                store=self.store,
                approval=approval or ApprovalPolicy(),
            ),
            outcome_evaluator=outcome_evaluator or DeterministicOutcomeEvaluator(),
            store=self.store,
            selector=CandidateSelector(beam_width=3),
            procedure_skills=procedure_skills,
            curator_daemon=curator_daemon,
        )

    def test_agent_loop_executes_only_one_action_per_step_and_achieves_goal(self):
        model = SequenceModel([[candidate()], [candidate(goal_achieving=True)]])

        result = self.agent_loop(model).run("collect evidence", max_steps=5, interactive=False)

        self.assertEqual(result.termination, TerminationReason.ACHIEVED)
        self.assertEqual(result.steps_executed, 2)
        self.assertEqual(len(self.executions), 2)
        journal = self.store.inspect_run(result.run_id)
        event_types = [event["event_type"] for event in journal["events"]]
        self.assertEqual(event_types.count("action_selected"), 2)
        self.assertEqual(event_types.count("observation"), 2)
        self.assertEqual(event_types.count("outcome_evaluated"), 2)

    def test_agent_loop_journals_non_secret_model_provider_context(self):
        model = SequenceModel([[candidate(goal_achieving=True)]])
        model.journal_context = {
            "model_provider": "ollama",
            "model": "qwen2.5vl:7b",
            "endpoint": "http://127.0.0.1:11434/v1",
            "configuration_source": "global",
        }

        result = self.agent_loop(model).run("collect evidence", interactive=False)

        started = self.store.inspect_run(result.run_id)["events"][0]["payload"]
        self.assertEqual(started["model_provider"], "ollama")
        self.assertEqual(started["configuration_source"], "global")
        self.assertEqual(started["interface"], "run")
        self.assertNotIn("api_key", started)

    def test_agent_loop_journals_chat_interface(self):
        model = SequenceModel([[candidate(goal_achieving=True)]])

        result = self.agent_loop(model).run("collect evidence", interactive=False, interface="chat")

        started = self.store.inspect_run(result.run_id)["events"][0]["payload"]
        self.assertEqual(started["interface"], "chat")

    def test_agent_loop_triggers_curator_daemon_after_run_finish(self):
        class FakeCuratorDaemon:
            def __init__(self):
                self.run_ids = []

            def trigger_after_run(self, run_id):
                self.run_ids.append(run_id)

        daemon = FakeCuratorDaemon()
        result = self.agent_loop(
            SequenceModel([[candidate(goal_achieving=True)]]),
            curator_daemon=daemon,
        ).run("collect evidence", interactive=False)

        self.assertEqual(daemon.run_ids, [result.run_id])

    def test_agent_loop_accepts_conversation_context_and_journal_metadata(self):
        class ContextModel(SequenceModel):
            def __init__(self):
                super().__init__([[candidate(goal_achieving=True)]])
                self.contexts = []

            def propose(self, state, available_tools, procedure_skills, tool_specs=None):
                self.contexts.append(state.conversation_context)
                return super().propose(state, available_tools, procedure_skills, tool_specs)

        model = ContextModel()

        result = self.agent_loop(model).run(
            "continue",
            interactive=False,
            interface="chat",
            conversation_context="previous run summary",
            journal_metadata={
                "conversation_session_id": "session",
                "conversation_turn_id": "turn",
            },
        )

        self.assertEqual(model.contexts, ["previous run summary"])
        started = self.store.inspect_run(result.run_id)["events"][0]["payload"]
        self.assertEqual(started["conversation_session_id"], "session")
        self.assertEqual(started["conversation_turn_id"], "turn")

    def test_agent_loop_passes_registry_tool_specs_to_model(self):
        class CapturingModel(SequenceModel):
            def __init__(self):
                super().__init__([[candidate(goal_achieving=True)]])
                self.tool_specs = []

            def propose(self, state, available_tools, procedure_skills, tool_specs=None):
                self.tool_specs.append(tool_specs or [])
                return super().propose(state, available_tools, procedure_skills, tool_specs)

        model = CapturingModel()

        result = self.agent_loop(model).run("collect evidence", interactive=False)

        self.assertEqual(result.termination, TerminationReason.ACHIEVED)
        self.assertEqual(model.tool_specs[0][0]["name"], "test.tool")
        self.assertEqual(model.tool_specs[0][0]["risk_level"], "low")

    def test_agent_loop_exposes_explicit_no_candidates_termination(self):
        result = self.agent_loop(SequenceModel([[]])).run("nothing available", interactive=False)

        self.assertEqual(result.termination, TerminationReason.NO_CANDIDATES)
        self.assertEqual(result.steps_executed, 0)

    def test_extra_model_governance_fields_do_not_drive_scoring(self):
        model = SequenceModel([[candidate(goal_achieving=True)]])

        result = self.agent_loop(model).run("governed goal", interactive=False)

        self.assertEqual(result.termination, TerminationReason.ACHIEVED)
        self.assertEqual(len(self.executions), 1)
        journal = self.store.inspect_run(result.run_id)
        ranked = next(event for event in journal["events"] if event["event_type"] == "candidates_ranked")
        self.assertNotIn("goal_progress", ranked["payload"][0]["score_details"])

    def test_tool_argument_validation_runs_at_execution_boundary(self):
        self.registry = build_local_tool_registry(self.tmpdir.name)
        model = SequenceModel([[candidate(tool="filesystem.list")]])
        model.candidates_by_call[0][0].actions[0] = ActionIntent(
            "filesystem.list",
            {"path": "/"},
            "list outside workspace",
        )

        result = self.agent_loop(model).run("stay inside workspace", interactive=False)

        self.assertEqual(result.termination, TerminationReason.NO_CANDIDATES)
        journal = self.store.inspect_run(result.run_id)
        blocked = next(
            event
            for event in journal["events"]
            if event["event_type"] == "execution_candidates_blocked"
        )
        self.assertIn("path escapes workspace", blocked["payload"][0]["reason"])
        self.assertNotIn("observation", [event["event_type"] for event in journal["events"]])

    def test_action_gateway_tries_next_ranked_candidate_when_first_fails_execution_boundary(self):
        self.registry = build_local_tool_registry(self.tmpdir.name)
        valid_dir = Path(self.tmpdir.name) / "valid"
        valid_dir.mkdir()
        invalid = CandidatePath(
            source="invalid",
            actions=[
                ActionIntent(
                    "filesystem.list",
                    {"path": "/"},
                    "list outside workspace",
                    evidence_strength=5.0,
                )
            ],
        )
        valid = CandidatePath(
            source="valid",
            actions=[
                ActionIntent(
                    "filesystem.list",
                    {"path": "valid"},
                    "list workspace path",
                )
            ],
        )
        result = self.agent_loop(SequenceModel([[invalid, valid]])).run(
            "stay inside workspace",
            max_steps=1,
            interactive=False,
        )

        self.assertEqual(result.steps_executed, 1)
        journal = self.store.inspect_run(result.run_id)
        blocked = next(
            event
            for event in journal["events"]
            if event["event_type"] == "execution_candidates_blocked"
        )
        selected = next(
            event for event in journal["events"] if event["event_type"] == "action_selected"
        )
        self.assertIn("path escapes workspace", blocked["payload"][0]["reason"])
        self.assertEqual(selected["payload"]["arguments"], {"path": "valid"})

    def test_agent_loop_executes_web_intent_through_gateway(self):
        class Headers:
            def get_content_charset(self):
                return "utf-8"

            def get(self, name, default=""):
                return "text/html; charset=utf-8" if name == "content-type" else default

        class Response:
            status = 200
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                del exc_type, exc, traceback

            def read(self, size=-1):
                body = b"<html><body>Example Domain</body></html>"
                return body if size < 0 else body[:size]

            def geturl(self):
                return "https://example.test/"

        self.registry = build_local_tool_registry(
            self.tmpdir.name,
            ToolsetConfiguration(enabled_toolsets=("web",)),
        )
        model = SequenceModel(
            [
                [
                    candidate(
                        tool="web.fetch",
                        arguments={
                            "url": "https://example.test/",
                            "_goal_achieving": True,
                        },
                        purpose="fetch target page",
                    )
                ]
            ]
        )

        with patch("urllib.request.urlopen", return_value=Response()):
            result = self.agent_loop(model).run("inspect website", interactive=False)

        self.assertEqual(result.termination, TerminationReason.ACHIEVED)
        journal = self.store.inspect_run(result.run_id)
        selected = next(
            event for event in journal["events"] if event["event_type"] == "action_selected"
        )
        self.assertEqual(selected["payload"]["tool"], "web.fetch")
        self.assertEqual(selected["payload"]["tool_spec"]["toolset"], "web")

    def test_non_interactive_mode_denies_action_that_requires_approval(self):
        self.registry.register(
            "high.tool",
            lambda arguments: Observation("", True),
            default_risk=RiskLevel.HIGH,
        )
        model = SequenceModel([[candidate(tool="high.tool")]])

        result = self.agent_loop(model).run("risky goal", interactive=False)

        self.assertEqual(result.termination, TerminationReason.APPROVAL_DENIED)
        self.assertEqual(self.executions, [])

    def test_non_interactive_mode_denies_file_write_without_modifying_workspace(self):
        self.registry = build_local_tool_registry(self.tmpdir.name)
        model = SequenceModel(
            [
                [
                    candidate(
                        tool="filesystem.write",
                        arguments={"path": "created.txt", "content": "nope\n"},
                        purpose="create file",
                    )
                ]
            ]
        )

        result = self.agent_loop(model).run("create a file", interactive=False)

        self.assertEqual(result.termination, TerminationReason.APPROVAL_DENIED)
        self.assertFalse((Path(self.tmpdir.name) / "created.txt").exists())
        journal = self.store.inspect_run(result.run_id)
        approval = next(
            event for event in journal["events"] if event["event_type"] == "approval_decision"
        )
        self.assertFalse(approval["payload"]["allowed"])

    def test_non_interactive_mode_denies_process_start_without_execution(self):
        self.registry = build_local_tool_registry(self.tmpdir.name)
        root = Path(self.tmpdir.name)
        model = SequenceModel(
            [
                [
                    candidate(
                        tool="process.start",
                        arguments={
                            "command": (
                                "python3.13 -c "
                                "\"from pathlib import Path; Path('started.txt').write_text('yes')\""
                            )
                        },
                        purpose="start a background command",
                    )
                ]
            ]
        )

        result = self.agent_loop(model).run("start a process", interactive=False)

        self.assertEqual(result.termination, TerminationReason.APPROVAL_DENIED)
        self.assertFalse((root / "started.txt").exists())
        journal = self.store.inspect_run(result.run_id)
        approval = next(
            event for event in journal["events"] if event["event_type"] == "approval_decision"
        )
        selected = next(
            event for event in journal["events"] if event["event_type"] == "action_selected"
        )
        self.assertFalse(approval["payload"]["allowed"])
        self.assertEqual(selected["payload"]["tool"], "process.start")
        self.assertEqual(selected["payload"]["risk_level"], "medium")

    def test_agent_loop_executes_approved_process_start_through_gateway(self):
        self.registry = build_local_tool_registry(self.tmpdir.name)
        approval = ApprovalPolicy(prompt=lambda message: "process.start" in message)
        model = SequenceModel(
            [
                [
                    candidate(
                        tool="process.start",
                        arguments={
                            "command": "python3.13 -c \"print('agent-process', flush=True)\"",
                            "_goal_achieving": True,
                        },
                        purpose="start a short background command",
                    )
                ]
            ]
        )

        result = self.agent_loop(model, approval=approval).run("start a process", interactive=True)

        self.assertEqual(result.termination, TerminationReason.ACHIEVED)
        journal = self.store.inspect_run(result.run_id)
        observation = next(
            event for event in journal["events"] if event["event_type"] == "observation"
        )
        selected = next(
            event for event in journal["events"] if event["event_type"] == "action_selected"
        )
        self.assertEqual(selected["payload"]["tool"], "process.start")
        self.assertIn("process_started:", observation["payload"]["evidence"][0])

    def test_agent_loop_executes_approved_file_write_through_gateway(self):
        self.registry = build_local_tool_registry(self.tmpdir.name)
        approval = ApprovalPolicy(prompt=lambda message: "filesystem.write" in message)
        model = SequenceModel(
            [
                [
                    candidate(
                        tool="filesystem.write",
                        arguments={
                            "path": "created.txt",
                            "content": "hello\n",
                            "_goal_achieving": True,
                        },
                        purpose="create a workspace text file",
                    )
                ]
            ]
        )

        result = self.agent_loop(model, approval=approval).run("create a file", interactive=True)

        self.assertEqual(result.termination, TerminationReason.ACHIEVED)
        self.assertEqual((Path(self.tmpdir.name) / "created.txt").read_text(encoding="utf-8"), "hello\n")
        journal = self.store.inspect_run(result.run_id)
        selected = next(
            event for event in journal["events"] if event["event_type"] == "action_selected"
        )
        self.assertEqual(selected["payload"]["tool"], "filesystem.write")
        self.assertEqual(selected["payload"]["risk_level"], "medium")

    def test_agent_loop_blocks_failed_file_patch_after_observation(self):
        root = Path(self.tmpdir.name)
        (root / "sample.txt").write_text("alpha\n", encoding="utf-8")
        self.registry = build_local_tool_registry(root)
        approval = ApprovalPolicy(prompt=lambda message: True)
        model = SequenceModel(
            [
                [
                    candidate(
                        tool="filesystem.patch",
                        arguments={
                            "path": "sample.txt",
                            "old_string": "missing",
                            "new_string": "replacement",
                        },
                        purpose="patch text",
                    )
                ]
            ]
        )

        result = self.agent_loop(model, approval=approval).run("patch a file", interactive=True)

        self.assertEqual(result.termination, TerminationReason.BLOCKED)
        self.assertEqual((root / "sample.txt").read_text(encoding="utf-8"), "alpha\n")
        journal = self.store.inspect_run(result.run_id)
        observation = next(
            event for event in journal["events"] if event["event_type"] == "observation"
        )
        self.assertIn("old_string was not found", observation["payload"]["error"])

    def test_agent_loop_exposes_max_steps_termination(self):
        result = self.agent_loop(
            SequenceModel([[candidate(nonce=1)], [candidate(nonce=2)]])
        ).run(
            "keep collecting",
            max_steps=2,
            interactive=False,
        )

        self.assertEqual(result.termination, TerminationReason.MAX_STEPS_REACHED)
        self.assertEqual(result.steps_executed, 2)

    def test_agent_loop_penalizes_but_does_not_reject_a_successful_action_repeat(self):
        repeated = candidate()

        result = self.agent_loop(SequenceModel([[repeated]])).run(
            "avoid loops",
            max_steps=3,
            interactive=False,
        )

        self.assertEqual(result.termination, TerminationReason.MAX_STEPS_REACHED)
        self.assertEqual(result.steps_executed, 3)
        self.assertEqual(len(self.executions), 3)
        journal = self.store.inspect_run(result.run_id)
        penalized_events = [
            event for event in journal["events"] if event["event_type"] == "candidates_penalized"
        ]
        self.assertTrue(
            any(
                "action already succeeded with accepted outcome in this run"
                in event["payload"][0]["reasons"]
                for event in penalized_events
                if event["payload"]
            )
        )

    def test_agent_loop_exposes_blocked_and_failed_terminations(self):
        def failing_tool(arguments):
            del arguments
            return Observation("", False, error="tool could not proceed")

        self.registry = ToolRegistry()
        self.registry.register("test.tool", failing_tool)
        blocked = self.agent_loop(SequenceModel([[candidate()]])).run("blocked", interactive=False)
        self.assertEqual(blocked.termination, TerminationReason.BLOCKED)

        class BrokenModel:
            def propose(self, state, available_tools, procedure_skills, tool_specs=None):
                del state, available_tools, procedure_skills, tool_specs
                raise RuntimeError("model unavailable")

        failed = self.agent_loop(BrokenModel()).run("failed", interactive=False)
        self.assertEqual(failed.termination, TerminationReason.FAILED)

    def test_model_assisted_outcome_evaluator_cannot_override_deterministic_failure(self):
        class OptimisticModel:
            calls = 0

            def evaluate_outcome(self, state, action, observation):
                self.calls += 1
                raise AssertionError("model must not evaluate failed execution")

        model = OptimisticModel()
        evaluator = ModelAssistedOutcomeEvaluator(model)
        intent = candidate().next_action
        action = Action(
            intent.tool,
            intent.arguments,
            intent.purpose,
            "agent-derived outcome",
            purpose=intent.purpose,
        )
        result = evaluator.evaluate(
            state=None,
            action=action,
            observation=Observation(action.id, False, error="exit code 1"),
        )

        self.assertFalse(result.execution_ok)
        self.assertEqual(result.goal_status, GoalStatus.BLOCKED)
        self.assertEqual(model.calls, 0)

    def test_successful_multi_step_run_creates_candidate_procedure_skill(self):
        library = ProcedureSkillLibrary(
            self.tmpdir.name,
            self.store,
            skills_dir=Path(self.tmpdir.name) / "global-skills",
            candidates_dir=Path(self.tmpdir.name) / "global-skill-candidates",
        )
        result = self.agent_loop(
            SequenceModel([[candidate()], [candidate(goal_achieving=True)]]),
            procedure_skills=library,
        ).run("learn procedure", max_steps=3, interactive=False)

        self.assertEqual(result.termination, TerminationReason.ACHIEVED)
        candidates = library.list_candidates()
        self.assertEqual([item["name"] for item in candidates], ["learned-procedure"])
        self.assertEqual(candidates[0]["source_run_id"], result.run_id)
        self.assertEqual(candidates[0]["source_workspace"], str(Path(self.tmpdir.name).resolve()))

    def test_single_step_success_does_not_create_candidate_procedure_skill(self):
        library = ProcedureSkillLibrary(
            self.tmpdir.name,
            self.store,
            skills_dir=Path(self.tmpdir.name) / "global-skills",
            candidates_dir=Path(self.tmpdir.name) / "global-skill-candidates",
        )
        result = self.agent_loop(
            SequenceModel([[candidate(goal_achieving=True)]]),
            procedure_skills=library,
        ).run("single step", interactive=False)

        self.assertEqual(result.termination, TerminationReason.ACHIEVED)
        self.assertEqual(library.list_candidates(), [])

    def test_progressive_disclosure_is_journaled_but_not_given_to_outcome_evaluator(self):
        skill_dir = Path(self.tmpdir.name) / "global-skills" / "test-procedure"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            """---
name: test-procedure
description: Test procedure.
version: 1.0.0
tags: [test]
platforms: [macos, linux, windows]
requires_tools: [test.tool]
---

# Secret Procedure

secret-procedure-body
""",
            encoding="utf-8",
        )
        library = ProcedureSkillLibrary(
            self.tmpdir.name,
            self.store,
            skills_dir=Path(self.tmpdir.name) / "global-skills",
            candidates_dir=Path(self.tmpdir.name) / "global-skill-candidates",
        )

        class CapturingOutcomeEvaluator:
            state_text = ""

            def evaluate(self, state, action, observation):
                del action, observation
                self.state_text = repr(state)
                return Outcome(
                    execution_ok=True,
                    goal_status=GoalStatus.ACHIEVED,
                    reason="captured",
                    evidence=("captured",),
                )

        outcome_evaluator = CapturingOutcomeEvaluator()
        result = self.agent_loop(
            SequenceModel([[candidate(goal_achieving=True)]]),
            outcome_evaluator=outcome_evaluator,
            procedure_skills=library,
        ).run("use procedure", interactive=False)

        journal = self.store.inspect_run(result.run_id)
        loaded = next(event for event in journal["events"] if event["event_type"] == "skills_loaded")
        self.assertEqual(loaded["payload"][0]["name"], "test-procedure")
        self.assertNotIn("secret-procedure-body", str(loaded["payload"]))
        self.assertNotIn("secret-procedure-body", outcome_evaluator.state_text)


if __name__ == "__main__":
    unittest.main()
