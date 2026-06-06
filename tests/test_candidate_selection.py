import unittest

from candidate_selection import BeamSearchSelector, CandidatePath, CandidateSource, CandidateStep


def _candidate(
    path_id,
    goal_progress,
    risk=0.0,
    verifiable=True,
    safety_allowed=True,
    permission_allowed=True,
):
    return CandidatePath(
        path_id=path_id,
        source=CandidateSource.SKILL_GRAPH,
        steps=[
            CandidateStep(
                skill_name=path_id,
                action="run {}".format(path_id),
                goal_progress=goal_progress,
                verifiability=1.0 if verifiable else 0.0,
                edge_confidence=0.8,
                evidence_strength=0.6,
                skill_availability=1.0,
                risk=risk,
                cost=0.1,
                uncertainty=0.1,
                verifiable=verifiable,
                safety_allowed=safety_allowed,
                permission_allowed=permission_allowed,
            )
        ],
    )



class CandidateSelectionTest(unittest.TestCase):
    def test_beam_search_keeps_top_three_after_penalty_scoring(self):
        candidates = [
            _candidate("best", 1.0, risk=0.0),
            _candidate("second", 0.8, risk=0.1),
            _candidate("third", 0.6, risk=0.1),
            _candidate("fourth", 0.4, risk=0.1),
            _candidate("unsafe", 1.0, risk=0.0, safety_allowed=False),
            _candidate("unverifiable", 1.0, risk=0.0, verifiable=False),
        ]

        selected = BeamSearchSelector(beam_width=3).select(candidates)

        self.assertEqual(
            [candidate.path_id for candidate in selected],
            ["best", "second", "third"],
        )
        self.assertTrue(all(candidate.score > 0 for candidate in selected))
        self.assertEqual(candidates[4].penalty_reasons, ["safety not allowed"])
        self.assertEqual(
            candidates[5].penalty_reasons,
            ["candidate is not externally verifiable"],
        )

    def test_utility_scoring_prefers_lower_risk_when_progress_is_equal(self):
        low_risk = _candidate("low-risk", 0.7, risk=0.1)
        high_risk = _candidate("high-risk", 0.7, risk=0.9)

        selected = BeamSearchSelector(beam_width=2).select([high_risk, low_risk])

        self.assertEqual(
            [candidate.path_id for candidate in selected],
            ["low-risk", "high-risk"],
        )
