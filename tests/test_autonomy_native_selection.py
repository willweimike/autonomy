import unittest

from autonomy import ActionIntent, CandidatePath, CandidateSelector, RiskLevel


def path(
    name,
    *,
    tool="test.tool",
    purpose="purpose",
    evidence_strength=0.0,
):
    return CandidatePath(
        source=name,
        actions=[
            ActionIntent(
                tool=tool,
                arguments={"name": name},
                purpose=purpose,
                evidence_strength=evidence_strength,
            )
        ],
    )


class AutonomyNativeSelectionTest(unittest.TestCase):
    def test_penalties_are_scored_before_top_three_selection(self):
        candidates = [
            path("best", evidence_strength=0.8),
            path("second", evidence_strength=0.7),
            path("third", evidence_strength=0.6),
            path("fourth", evidence_strength=0.1),
            path("unknown-tool", tool="missing", evidence_strength=1.0),
            path("no-purpose", purpose="", evidence_strength=0.4),
        ]

        selected = CandidateSelector(beam_width=3).select(candidates, {"test.tool"})

        self.assertEqual([item.source for item in selected], ["best", "second", "third"])
        self.assertEqual(candidates[4].penalty_reasons, ["tool is unavailable: missing"])
        self.assertEqual(candidates[5].penalty_reasons, [])
        self.assertEqual(candidates[4].rejection_reason, "")

    def test_deduplicates_equivalent_actions(self):
        first = path("first-source")
        duplicate = path("duplicate-source", evidence_strength=0.9)
        duplicate.actions[0] = ActionIntent(
            tool=first.actions[0].tool,
            arguments=first.actions[0].arguments,
            purpose="different prose",
        )

        selected = CandidateSelector().select([first, duplicate], {"test.tool"})

        self.assertEqual(len(selected), 1)

    def test_penalizes_an_action_already_successful_in_the_run(self):
        repeated = path("repeated")

        selected = CandidateSelector().select(
            [repeated],
            {"test.tool"},
            {repeated.next_action.fingerprint},
        )

        self.assertEqual([item.source for item in selected], ["repeated"])
        self.assertEqual(
            repeated.penalty_reasons,
            ["action already succeeded with accepted outcome in this run"],
        )
        self.assertGreater(repeated.score_details["penalty"], 0)

    def test_penalizes_repeated_failed_or_non_ok_action(self):
        repeated = path("repeated-failure", evidence_strength=1.0)
        alternative = path("alternative", evidence_strength=0.2)

        selected = CandidateSelector(beam_width=2).select(
            [repeated, alternative],
            {"test.tool"},
            failed_action_counts={repeated.next_action.fingerprint: 2},
        )

        self.assertEqual([item.source for item in selected], ["alternative", "repeated-failure"])
        self.assertEqual(
            repeated.penalty_reasons,
            ["action already failed or produced non-ok outcome in this run:2"],
        )
        self.assertEqual(repeated.score_details["penalty"], 2.0)

    def test_applies_tool_argument_validation_as_a_penalty(self):
        invalid = path("invalid-arguments")

        selected = CandidateSelector().select(
            [invalid],
            {"test.tool"},
            action_rejection_reason=lambda action: "invalid tool arguments",
        )

        self.assertEqual([item.source for item in selected], ["invalid-arguments"])
        self.assertEqual(invalid.penalty_reasons, ["invalid tool arguments"])

    def test_uses_gateway_risk_instead_of_model_scoring_fields(self):
        low = path("low", evidence_strength=0.5)
        high = path("high", evidence_strength=0.5)

        selected = CandidateSelector().select(
            [high, low],
            {"test.tool"},
            action_risk=lambda action: (
                RiskLevel.HIGH if action.arguments["name"] == "high" else RiskLevel.LOW
            ),
        )

        self.assertEqual([item.source for item in selected], ["low", "high"])
        self.assertIn("risk", low.score_details)


if __name__ == "__main__":
    unittest.main()
