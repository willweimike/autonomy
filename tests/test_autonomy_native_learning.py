import tempfile
import unittest
from pathlib import Path

from autonomy import (
    Action,
    AutonomyStore,
    LearningLoop,
    LearningProposalType,
    Observation,
    ProcedureSkillDraft,
    ProcedureSkillLibrary,
    SkillCurator,
    TerminationReason,
)
from autonomy.models import Goal, GoalStatus, Outcome, RunState, Transition


class DraftModel:
    def __init__(self):
        self.draft_calls = 0

    def draft_procedure_skill(self, state):
        self.draft_calls += 1
        return ProcedureSkillDraft(
            name="learned-skill",
            description=f"Learned from {state.goal.text}",
            body="# Learned\n\nFollow the successful path.",
            requires_tools=("filesystem.list",),
        )


def skill_md(
    *,
    name,
    description,
    body,
    requires_tools=("filesystem.list",),
    platforms=("macos", "linux", "windows"),
):
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "version: 1.0.0\n"
        "tags: [testing]\n"
        f"platforms: [{', '.join(platforms)}]\n"
        f"requires_tools: [{', '.join(requires_tools)}]\n"
        "---\n\n"
        f"{body.strip()}\n"
    )


class AutonomyNativeLearningTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmpdir.name) / "workspace"
        self.workspace.mkdir()
        self.store = AutonomyStore(Path(self.tmpdir.name) / "autonomy.db")
        self.skills_dir = Path(self.tmpdir.name) / "skills"
        self.candidates_dir = Path(self.tmpdir.name) / "skill-candidates"
        self.library = ProcedureSkillLibrary(
            self.workspace,
            self.store,
            skills_dir=self.skills_dir,
            candidates_dir=self.candidates_dir,
        )

    def tearDown(self):
        self.tmpdir.cleanup()

    def state_with_transitions(self, count):
        state = RunState("run-1", Goal("inspect repository"))
        for step in range(1, count + 1):
            action = Action("filesystem.list", {"path": "."}, "list files", "runtime")
            state.transitions.append(
                Transition(
                    state.run_id,
                    step,
                    action,
                    Observation(action.id, True, output="ok", evidence=("ok",)),
                    Outcome(True, GoalStatus.CONTINUE, "ok"),
                )
            )
        state.step = count
        self.store.create_run(state.run_id, state.goal.text)
        return state

    def test_learning_loop_creates_new_skill_candidate_for_multistep_achieved_run(self):
        model = DraftModel()
        state = self.state_with_transitions(2)

        proposals = LearningLoop(
            model=model,
            store=self.store,
            procedure_skills=self.library,
        ).review_run(
            state,
            termination=TerminationReason.ACHIEVED,
            reason="done",
        )

        self.assertEqual(model.draft_calls, 1)
        self.assertEqual(proposals[0].proposal_type, LearningProposalType.NEW_SKILL)
        candidates = self.library.list_candidates()
        self.assertEqual(candidates[0]["name"], "learned-skill")
        self.assertEqual(candidates[0]["proposal_type"], "new_skill")
        self.assertEqual(candidates[0]["source_run_id"], "run-1")
        self.assertIn("successful outcomes", candidates[0]["reason"])

    def test_learning_loop_records_no_learning_for_single_step_achieved_run(self):
        model = DraftModel()
        state = self.state_with_transitions(1)

        proposals = LearningLoop(
            model=model,
            store=self.store,
            procedure_skills=self.library,
        ).review_run(
            state,
            termination=TerminationReason.ACHIEVED,
            reason="done",
        )

        self.assertEqual(model.draft_calls, 0)
        self.assertEqual(proposals[0].proposal_type, LearningProposalType.NO_LEARNING)
        self.assertEqual(self.library.list_candidates(), [])

    def test_blocked_run_with_successful_outcome_records_review_without_candidate(self):
        model = DraftModel()
        state = self.state_with_transitions(1)

        proposals = LearningLoop(
            model=model,
            store=self.store,
            procedure_skills=self.library,
        ).review_run(
            state,
            termination=TerminationReason.BLOCKED,
            reason="blocked",
        )

        self.assertEqual(model.draft_calls, 0)
        self.assertEqual(proposals[0].proposal_type, LearningProposalType.NO_LEARNING)
        self.assertIn("patch proposals", proposals[0].reason)
        self.assertEqual(self.library.list_candidates(), [])

    def write_skill(self, name, body, **kwargs):
        skill_dir = self.skills_dir / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            skill_md(name=name, description=name, body=body, **kwargs),
            encoding="utf-8",
        )

    def test_curator_auto_merges_duplicate_skill_and_deletes_source(self):
        body = "# Test Diagnosis\n\nList files before searching."
        self.write_skill("test-diagnosis", body)
        self.write_skill("test-diagnosis-copy", body)
        curator = SkillCurator(self.library, self.store)

        results = curator.apply_auto_merges()

        self.assertEqual(len(results), 1)
        self.assertFalse((self.skills_dir / results[0]["source_skill"]).exists())
        self.assertTrue((self.skills_dir / results[0]["target_skill"] / "SKILL.md").is_file())
        self.assertNotIn("absorbed", (self.skills_dir / results[0]["target_skill"] / "SKILL.md").read_text())
        self.assertEqual(self.store.list_curator_events()[0]["event_type"], "curator_merge")

    def test_curator_auto_merges_subcase_without_runtime_lineage(self):
        source_body = "Search for pytest failure messages."
        target_body = "# Test Diagnosis\n\nList files.\n\nSearch for pytest failure messages."
        self.write_skill("pytest-failure", source_body)
        self.write_skill("test-diagnosis", target_body)
        curator = SkillCurator(self.library, self.store)

        results = curator.apply_auto_merges()

        self.assertEqual(results[0]["source_skill"], "pytest-failure")
        self.assertFalse((self.skills_dir / "pytest-failure").exists())
        target_text = (self.skills_dir / "test-diagnosis" / "SKILL.md").read_text()
        self.assertNotIn("pytest-failure", target_text)
        self.assertNotIn("absorbed", target_text)

    def test_curator_does_not_merge_when_tools_or_platforms_expand_or_disabled(self):
        source_body = "Search for pytest failure messages."
        target_body = "# Test Diagnosis\n\nSearch for pytest failure messages."
        self.write_skill("shell-case", source_body, requires_tools=("shell.execute",))
        self.write_skill("test-diagnosis", target_body, requires_tools=("filesystem.list",))
        self.write_skill("disabled-case", source_body)
        self.library.index({"filesystem.list", "shell.execute"}, include_disabled=True)
        self.store.set_procedure_skill_enabled("disabled-case", False)

        results = SkillCurator(self.library, self.store).apply_auto_merges()

        self.assertEqual(results, [])
        self.assertTrue((self.skills_dir / "shell-case" / "SKILL.md").is_file())
        self.assertTrue((self.skills_dir / "disabled-case" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
