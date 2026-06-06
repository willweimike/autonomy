import unittest

from autonomy_core import AutonomyCore, AutonomyState, ExecutionResult
from autonomy_core.models import ActivationState
from candidate_selection import CandidatePath, CandidateSource, CandidateStatus, CandidateStep


class StaticProvider:
    def __init__(self, candidates):
        self.candidates = candidates

    def generate(self, state):
        return list(self.candidates)



class AutonomyCoreTest(unittest.TestCase):
    def test_autonomy_core_activates_only_the_next_step(self):
        candidate = CandidatePath(
            path_id="two-step-path",
            source=CandidateSource.MODEL_PROPOSED,
            steps=[
                CandidateStep(
                    skill_name="repo_orientation",
                    action="inspect repository",
                    goal_progress=0.7,
                    verifiability=1.0,
                    risk=0.1,
                ),
                CandidateStep(
                    skill_name="implementation",
                    action="write code",
                    goal_progress=1.0,
                    verifiability=1.0,
                    risk=0.3,
                ),
            ],
        )
        core = AutonomyCore(providers=[StaticProvider([candidate])])

        activation = core.activate_next(
            AutonomyState(goal="implement feature", current_state="unknown repo")
        )

        self.assertEqual(activation.state, ActivationState.ACTIVATED)
        self.assertEqual(activation.candidate.status, CandidateStatus.ACTIVATED)
        self.assertEqual(activation.activated_step.skill_name, "repo_orientation")

    def test_autonomy_core_requires_explicit_termination_state(self):
        candidate = CandidatePath(
            path_id="verified-path",
            source=CandidateSource.POLICY_DEFAULT,
            steps=[
                CandidateStep(
                    skill_name="test_execution",
                    action="run tests",
                    goal_progress=1.0,
                    verifiability=1.0,
                )
            ],
        )
        core = AutonomyCore(providers=[StaticProvider([candidate])])
        activation = core.activate_next(
            AutonomyState(goal="verify code", current_state="code changed")
        )

        closed = core.close_activation(
            activation,
            ExecutionResult(
                succeeded=True,
                externally_verified=True,
                reason="tests passed",
                evidence="pytest passed",
            ),
        )

        self.assertEqual(closed.state, ActivationState.COMPLETED)
        self.assertEqual(closed.candidate.status, CandidateStatus.COMPLETED)

    def test_autonomy_core_pauses_success_without_external_verification(self):
        candidate = CandidatePath(
            path_id="unverified-success",
            source=CandidateSource.POLICY_DEFAULT,
            steps=[CandidateStep("draft_report", "write draft", goal_progress=0.8)],
        )
        core = AutonomyCore(providers=[StaticProvider([candidate])])
        activation = core.activate_next(
            AutonomyState(goal="create report", current_state="draft needed")
        )

        closed = core.close_activation(
            activation,
            ExecutionResult(
                succeeded=True,
                externally_verified=False,
                reason="draft exists but user has not accepted it",
            ),
        )

        self.assertEqual(closed.state, ActivationState.PAUSED)
        self.assertEqual(closed.candidate.status, CandidateStatus.PAUSED)
