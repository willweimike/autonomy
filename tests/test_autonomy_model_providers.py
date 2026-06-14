import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autonomy import (
    ModelConfigStore,
    ModelConfiguration,
    OpenAICompatibleProvider,
    ProviderConfigurationError,
)
from autonomy.providers import create_provider


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class AutonomyModelProviderTest(unittest.TestCase):
    def test_config_store_round_trips_without_secret_in_yaml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModelConfigStore(Path(tmpdir))
            configuration = ModelConfiguration(
                "openai-api",
                "gpt-test",
                "https://api.openai.com/v1",
                60,
            )
            store.save(configuration, openai_api_key="secret-value")

            self.assertEqual(store.load(), configuration)
            self.assertEqual(store.load_openai_api_key(), "secret-value")
            self.assertNotIn("secret-value", store.config_path.read_text(encoding="utf-8"))
            self.assertEqual(store.env_path.stat().st_mode & 0o777, 0o600)

    def test_legacy_environment_variables_are_not_configuration_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "AUTONOMY_MODEL": "legacy-model",
                "AUTONOMY_API_KEY": "legacy-secret",
                "AUTONOMY_BASE_URL": "http://legacy/v1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ProviderConfigurationError, "autonomy model setup"):
                ModelConfigStore(Path(tmpdir)).load()

    def test_ollama_configuration_requires_v1(self):
        configuration = ModelConfiguration(
            "ollama",
            "qwen2.5vl:7b",
            "http://127.0.0.1:11434",
            180,
        )
        with self.assertRaisesRegex(ProviderConfigurationError, "include /v1"):
            configuration.validate()

    def test_configuration_rejects_boolean_timeout(self):
        configuration = ModelConfiguration(
            "ollama",
            "qwen2.5vl:7b",
            "http://127.0.0.1:11434/v1",
            True,
        )
        with self.assertRaisesRegex(ProviderConfigurationError, "positive integer"):
            configuration.validate()

    def test_create_provider_uses_workspace_configuration_without_exposing_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModelConfigStore(Path(tmpdir))
            configuration = ModelConfiguration(
                "openai-api",
                "gpt-test",
                "https://api.openai.com/v1",
                60,
            )
            store.save(configuration, openai_api_key="secret-value")
            provider = create_provider(configuration, store)

        self.assertEqual(
            provider.journal_context,
            {
                "model_provider": "openai-api",
                "model": "gpt-test",
                "endpoint": "https://api.openai.com/v1",
                "configuration_source": "workspace",
            },
        )
        self.assertNotIn("secret-value", repr(provider.journal_context))

    def test_provider_validate_checks_model_and_json_object_response(self):
        provider = OpenAICompatibleProvider(
            "ollama",
            "qwen2.5vl:7b",
            "ollama",
            "http://127.0.0.1:11434/v1",
            180,
        )
        responses = [
            Response(json.dumps({"data": [{"id": "qwen2.5vl:7b"}]}).encode()),
            Response(
                json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
            ),
        ]
        with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            provider.validate()

        request = urlopen.call_args_list[1].args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
