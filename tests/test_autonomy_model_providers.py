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
from autonomy.providers import PROVIDER_SPECS, create_provider


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

    def test_nvidia_provider_spec_matches_build_endpoint(self):
        spec = PROVIDER_SPECS["nvidia"]

        self.assertEqual(spec.default_base_url, "https://integrate.api.nvidia.com/v1")
        self.assertEqual(spec.default_model, "moonshotai/kimi-k2.6")
        self.assertEqual(spec.api_key_name, "NVIDIA_API_KEY")
        self.assertFalse(spec.supports_model_listing)

    def test_hermes_openai_compatible_provider_specs_are_available(self):
        expected = {
            "openrouter": (
                "https://openrouter.ai/api/v1",
                "OPENROUTER_API_KEY",
                "anthropic/claude-sonnet-4.6",
            ),
            "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
            "xai": ("https://api.x.ai/v1", "XAI_API_KEY", ""),
            "zai": ("https://api.z.ai/api/paas/v4", "GLM_API_KEY", "glm-5"),
            "kimi-coding": (
                "https://api.moonshot.ai/v1",
                "KIMI_API_KEY",
                "kimi-k2-turbo-preview",
            ),
            "alibaba": (
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                "DASHSCOPE_API_KEY",
                "",
            ),
        }

        for provider_id, (base_url, api_key_name, default_model) in expected.items():
            with self.subTest(provider_id=provider_id):
                spec = PROVIDER_SPECS[provider_id]
                self.assertEqual(spec.default_base_url, base_url)
                self.assertEqual(spec.api_key_name, api_key_name)
                self.assertEqual(spec.default_model, default_model)
                self.assertTrue(spec.requires_api_key)
                self.assertTrue(spec.supports_model_listing)

    def test_nvidia_key_round_trips_without_openai_secret_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModelConfigStore(Path(tmpdir))
            configuration = ModelConfiguration(
                "nvidia",
                "moonshotai/kimi-k2.6",
                "https://integrate.api.nvidia.com/v1",
                120,
            )
            store.save(configuration, api_key="nvidia-secret")

            self.assertEqual(store.load(), configuration)
            self.assertEqual(store.load_api_key("nvidia"), "nvidia-secret")
            self.assertEqual(store.existing_openai_api_key(), "")
            self.assertNotIn("nvidia-secret", store.config_path.read_text(encoding="utf-8"))
            self.assertIn("NVIDIA_API_KEY=", store.env_path.read_text(encoding="utf-8"))
            self.assertNotIn("AUTONOMY_OPENAI_API_KEY", store.env_path.read_text(encoding="utf-8"))

    def test_hermes_provider_keys_round_trip_under_native_env_names(self):
        for provider_id in (
            "openrouter",
            "deepseek",
            "xai",
            "zai",
            "kimi-coding",
            "alibaba",
        ):
            with self.subTest(provider_id=provider_id), tempfile.TemporaryDirectory() as tmpdir:
                store = ModelConfigStore(Path(tmpdir))
                spec = PROVIDER_SPECS[provider_id]
                configuration = ModelConfiguration(
                    provider_id,
                    spec.default_model or "test-model",
                    spec.default_base_url,
                    spec.default_timeout,
                )
                store.save(configuration, api_key=f"{provider_id}-secret")

                self.assertEqual(store.load_api_key(provider_id), f"{provider_id}-secret")
                self.assertIn(f"{spec.api_key_name}=", store.env_path.read_text(encoding="utf-8"))
                self.assertNotIn(
                    f"{provider_id}-secret",
                    store.config_path.read_text(encoding="utf-8"),
                )

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

    def test_create_provider_uses_nvidia_secret_and_skips_model_listing_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ModelConfigStore(Path(tmpdir))
            configuration = ModelConfiguration(
                "nvidia",
                "moonshotai/kimi-k2.6",
                "https://integrate.api.nvidia.com/v1",
                120,
            )
            store.save(configuration, api_key="nvidia-secret")
            provider = create_provider(configuration, store)

        response = Response(
            json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
        )
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            provider.validate()

        self.assertEqual(len(urlopen.call_args_list), 1)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://integrate.api.nvidia.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer nvidia-secret")

    def test_https_requests_use_certifi_ca_bundle_when_no_ssl_env_override(self):
        provider = OpenAICompatibleProvider(
            "nvidia",
            "moonshotai/kimi-k2.6",
            "nvidia-secret",
            "https://integrate.api.nvidia.com/v1",
            120,
            validate_model_listing=False,
        )
        response = Response(
            json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode()
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("autonomy.providers.certifi.where", return_value="/certifi/cacert.pem"),
            patch("autonomy.providers.ssl.create_default_context") as create_context,
            patch("urllib.request.urlopen", return_value=response) as urlopen,
        ):
            create_context.return_value = object()
            provider.complete_json({"messages": []})

        create_context.assert_called_once_with(cafile="/certifi/cacert.pem")
        self.assertIs(urlopen.call_args.kwargs["context"], create_context.return_value)

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

    def test_provider_recovers_json_from_fenced_content(self):
        provider = OpenAICompatibleProvider(
            "ollama",
            "qwen2.5vl:7b",
            "ollama",
            "http://127.0.0.1:11434/v1",
            180,
        )
        response = Response(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '```json\n{"ok": true}\n```',
                            }
                        }
                    ]
                }
            ).encode()
        )
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(provider.complete_json({"messages": []}), {"ok": True})

    def test_provider_recovers_json_from_surrounding_prose(self):
        provider = OpenAICompatibleProvider(
            "ollama",
            "qwen2.5vl:7b",
            "ollama",
            "http://127.0.0.1:11434/v1",
            180,
        )
        response = Response(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": 'Here is the JSON:\n{"ok": true}\nDone.',
                            }
                        }
                    ]
                }
            ).encode()
        )
        with patch("urllib.request.urlopen", return_value=response):
            self.assertEqual(provider.complete_json({"messages": []}), {"ok": True})


if __name__ == "__main__":
    unittest.main()
