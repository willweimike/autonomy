import json
import socket
import urllib.error
import unittest
from unittest.mock import patch

from autonomy import (
    Action,
    ModelClientError,
    Observation,
    OpenAICompatibleModel,
    ProcedureSkill,
    ProcedureSkillSummary,
)
from autonomy.models import Goal, RunState, Transition, Verification


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class AutonomyNativeModelTest(unittest.TestCase):
    def setUp(self):
        self.model = OpenAICompatibleModel(
            "qwen2.5vl:7b",
            "ollama",
            "http://127.0.0.1:11434/v1",
        )

    def test_list_models_reads_openai_compatible_endpoint(self):
        payload = json.dumps({"data": [{"id": "qwen2.5vl:7b"}]}).encode()
        with patch("urllib.request.urlopen", return_value=Response(payload)) as urlopen:
            models = self.model.list_models()

        self.assertEqual(models, ["qwen2.5vl:7b"])
        self.assertEqual(urlopen.call_args.args[0].full_url, "http://127.0.0.1:11434/v1/models")

    def test_endpoint_connection_error_is_clear(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaisesRegex(ModelClientError, "endpoint is unreachable"):
                self.model.list_models()

    def test_endpoint_timeout_is_clear(self):
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaisesRegex(ModelClientError, "endpoint is unreachable"):
                self.model.list_models()

    def test_endpoint_invalid_json_is_clear(self):
        with patch("urllib.request.urlopen", return_value=Response(b"not-json")):
            with self.assertRaisesRegex(ModelClientError, "endpoint returned invalid JSON"):
                self.model.list_models()

    def test_candidate_response_requires_valid_shape(self):
        with self.assertRaisesRegex(ModelClientError, "candidates must be an array"):
            self.model._parse_candidates({"candidates": "invalid"})

    def test_candidate_parser_accepts_action_intent_only_and_ignores_governance_fields(self):
        candidates = self.model._parse_candidates(
            {
                "candidates": [
                    {
                        "source": "model",
                        "actions": [
                            {
                                "tool": "filesystem.read",
                                "arguments": {"path": "README.md"},
                                "purpose": "read project overview",
                                "risk_level": "high",
                                "goal_progress": 1.0,
                                "cost": 99,
                                "uncertainty": 0,
                                "verification_plan": "trust me",
                            }
                        ],
                    }
                ]
            }
        )

        intent = candidates[0].next_action
        self.assertEqual(intent.tool, "filesystem.read")
        self.assertEqual(intent.arguments, {"path": "README.md"})
        self.assertEqual(intent.purpose, "read project overview")
        self.assertFalse(hasattr(intent, "risk_level"))
        self.assertFalse(hasattr(intent, "goal_progress"))

    def test_chat_completion_requires_valid_json_content(self):
        payload = json.dumps(
            {"choices": [{"message": {"content": "not-json"}}]}
        ).encode()
        with patch("urllib.request.urlopen", return_value=Response(payload)):
            with self.assertRaisesRegex(ModelClientError, "model returned invalid JSON content"):
                self.model._complete_json({"messages": []}, self.model._candidate_schema())

    def test_chat_completion_reports_missing_fields(self):
        with patch("urllib.request.urlopen", return_value=Response(b'{"choices": []}')):
            with self.assertRaisesRegex(ModelClientError, "chat completion response is invalid"):
                self.model._complete_json({"messages": []}, self.model._candidate_schema())

    def test_verification_reports_missing_fields(self):
        state = RunState("run", Goal("goal"))
        action = Action("filesystem.read", {"path": "README.md"}, "read", "verify")
        observation = Observation(action.id, True, output="read")
        with patch.object(self.model, "_complete_json", return_value={"verified": True}):
            with self.assertRaisesRegex(ModelClientError, "verification response is invalid"):
                self.model.verify(state=state, action=action, observation=observation)

    def test_skill_selection_ignores_unknown_names_and_limits_to_three(self):
        index = [
            ProcedureSkillSummary(
                name=name,
                description=name,
                version="1.0.0",
                tags=(),
                platforms=(),
                requires_tools=(),
                source="global",
                path=f"/{name}/SKILL.md",
                file_hash=name,
            )
            for name in ("one", "two", "three", "four")
        ]
        with patch.object(
            self.model,
            "_complete_json",
            return_value={"selected_skill_names": ["one", "unknown", "two", "three", "four"]},
        ):
            selected = self.model.select_procedure_skills(
                RunState("run", Goal("goal")),
                index,
                {"filesystem.read"},
            )

        self.assertEqual(selected, ["one", "two"])

    def test_propose_receives_only_loaded_procedure_skill_content(self):
        summary = ProcedureSkillSummary(
            "selected",
            "selected description",
            "1.0.0",
            (),
            (),
            (),
            "global",
            "/selected/SKILL.md",
            "hash",
        )
        selected = ProcedureSkill(summary, "selected instructions", "raw")
        captured = {}

        def complete(payload, schema):
            del schema
            captured.update(payload)
            return {"candidates": []}

        with patch.object(self.model, "_complete_json", side_effect=complete):
            self.model.propose(
                RunState("run", Goal("goal")),
                {"filesystem.read"},
                [selected],
            )

        user_payload = json.loads(captured["messages"][1]["content"])
        self.assertEqual(
            user_payload["tool_contracts"],
            {"filesystem.read": {"path": "string"}},
        )
        self.assertEqual(
            user_payload["procedure_skills"],
            [
                {
                    "name": "selected",
                    "description": "selected description",
                    "instructions": "selected instructions",
                }
            ],
        )

    def test_candidate_schema_limits_tools_to_available_set(self):
        schema = self.model._candidate_schema({"filesystem.read", "search.text"})

        tool_schema = schema["properties"]["candidates"]["items"]["properties"]["actions"][
            "items"
        ]["properties"]["tool"]
        self.assertEqual(tool_schema["enum"], ["filesystem.read", "search.text"])

    def test_propose_receives_recent_transition_evidence(self):
        state = RunState("run", Goal("goal"))
        action = Action("filesystem.list", {"path": "."}, "list", "verify")
        state.transitions.append(
            Transition(
                state.run_id,
                1,
                action,
                Observation(action.id, True, output="README.md", evidence=("listed:.",)),
                Verification(True, False, True, "README exists", progress=0.25),
            )
        )
        captured = {}

        def complete(payload, schema):
            del schema
            captured.update(payload)
            return {"candidates": []}

        with patch.object(self.model, "_complete_json", side_effect=complete):
            self.model.propose(state, {"filesystem.list"}, [])

        transition = json.loads(captured["messages"][1]["content"])["recent_transitions"][0]
        self.assertEqual(
            transition["action"],
            {"tool": "filesystem.list", "arguments": {"path": "."}, "purpose": ""},
        )
        self.assertEqual(transition["observation"]["output"], "README.md")


if __name__ == "__main__":
    unittest.main()
