import unittest

from autonomy import Action, DeterministicOutcomeEvaluator, Observation
from autonomy.models import Goal, GoalStatus, RunState


class AutonomyNativeOutcomeTest(unittest.TestCase):
    def test_failed_observation_reason_includes_tool_exit_code_and_output(self):
        evaluator = DeterministicOutcomeEvaluator()
        state = RunState("run-1", Goal("send email"))
        action = Action("shell.execute", {"command": "false"}, "check mail tool", "verify")
        observation = Observation(
            action.id,
            False,
            output="/usr/sbin/sendmail\n/usr/bin/mail\n",
            error="",
            evidence=("exit_code:1",),
            exit_code=1,
        )

        outcome = evaluator.evaluate(state, action, observation)

        self.assertFalse(outcome.execution_ok)
        self.assertEqual(outcome.goal_status, GoalStatus.BLOCKED)
        self.assertIn("shell.execute failed", outcome.reason)
        self.assertIn("exit_code 1", outcome.reason)
        self.assertIn("/usr/sbin/sendmail", outcome.reason)


if __name__ == "__main__":
    unittest.main()
