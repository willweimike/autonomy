from __future__ import annotations

import json
import os
import shlex
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml


class ModelClientError(RuntimeError):
    """A clear, user-facing model provider failure."""


class ProviderConfigurationError(ValueError):
    """An invalid or incomplete global model provider configuration."""


class ModelProvider(Protocol):
    provider_id: str
    model: str
    base_url: str
    timeout: int

    def complete_json(self, payload: dict, schema: dict | None = None) -> dict:
        ...

    def list_models(self) -> list[str]:
        ...

    def validate(self) -> None:
        ...

    @property
    def journal_context(self) -> dict[str, str]:
        ...


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    default_base_url: str
    default_timeout: int
    requires_api_key: bool


PROVIDER_SPECS = {
    "ollama": ProviderSpec(
        provider_id="ollama",
        default_base_url="http://127.0.0.1:11434/v1",
        default_timeout=180,
        requires_api_key=False,
    ),
    "openai-api": ProviderSpec(
        provider_id="openai-api",
        default_base_url="https://api.openai.com/v1",
        default_timeout=60,
        requires_api_key=True,
    ),
}


@dataclass(frozen=True)
class ModelConfiguration:
    provider: str
    model: str
    base_url: str
    timeout: int

    def validate(self) -> None:
        if not isinstance(self.provider, str) or self.provider not in PROVIDER_SPECS:
            raise ProviderConfigurationError(f"unsupported model provider: {self.provider}")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ProviderConfigurationError("configured model must not be empty")
        if not isinstance(self.base_url, str):
            raise ProviderConfigurationError("model base URL must be a string")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProviderConfigurationError(
                f"model base URL must be an absolute HTTP(S) URL: {self.base_url}"
            )
        if self.provider == "ollama" and not self.base_url.rstrip("/").endswith("/v1"):
            raise ProviderConfigurationError("Ollama base URL must include /v1")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, int) or self.timeout < 1:
            raise ProviderConfigurationError("model timeout must be a positive integer")

    def as_document(self) -> dict:
        return {
            "version": 1,
            "model": {
                "provider": self.provider,
                "model": self.model,
                "base_url": self.base_url.rstrip("/"),
                "timeout": self.timeout,
            },
        }


class ModelConfigStore:
    OPENAI_API_KEY_NAME = "AUTONOMY_OPENAI_API_KEY"

    def __init__(self, config_dir: Path | None = None):
        self.config_dir = (config_dir or Path.cwd() / ".autonomy").expanduser()
        self.config_path = self.config_dir / "config.yaml"
        self.env_path = self.config_dir / ".env"

    def load(self) -> ModelConfiguration:
        if not self.config_path.is_file():
            raise ProviderConfigurationError(
                "model provider is not configured; run `autonomy model setup`"
            )
        try:
            document = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ProviderConfigurationError(f"could not read model configuration: {exc}") from exc
        try:
            if not isinstance(document, dict) or document.get("version") != 1:
                raise TypeError("version must be 1")
            model = document["model"]
            if not isinstance(model, dict):
                raise TypeError("model must be an object")
            configuration = ModelConfiguration(
                provider=self._required_string(model, "provider"),
                model=self._required_string(model, "model"),
                base_url=self._required_string(model, "base_url").rstrip("/"),
                timeout=self._positive_int(model, "timeout"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderConfigurationError(f"model configuration is invalid: {exc}") from exc
        configuration.validate()
        return configuration

    def load_openai_api_key(self) -> str:
        secrets = self._read_secrets()
        api_key = secrets.get(self.OPENAI_API_KEY_NAME, "")
        if not api_key:
            raise ProviderConfigurationError(
                f"OpenAI API key is missing from {self.env_path}; run `autonomy model setup openai-api`"
            )
        return api_key

    def existing_openai_api_key(self) -> str:
        return self._read_secrets().get(self.OPENAI_API_KEY_NAME, "")

    def env_permissions_secure(self) -> bool | None:
        if not self.env_path.exists():
            return None
        return self.env_path.stat().st_mode & 0o777 == 0o600

    def save(
        self,
        configuration: ModelConfiguration,
        *,
        openai_api_key: str | None = None,
    ) -> None:
        configuration.validate()
        if configuration.provider == "openai-api" and not (
            openai_api_key or self.existing_openai_api_key()
        ):
            raise ProviderConfigurationError("OpenAI API key must not be empty")

        self.config_dir.mkdir(parents=True, exist_ok=True)
        if openai_api_key is not None:
            if not openai_api_key:
                raise ProviderConfigurationError("OpenAI API key must not be empty")
            self._atomic_write(
                self.env_path,
                f"{self.OPENAI_API_KEY_NAME}={json.dumps(openai_api_key)}\n",
                mode=0o600,
            )
        elif self.env_path.exists():
            os.chmod(self.env_path, 0o600)

        document = yaml.safe_dump(
            configuration.as_document(),
            sort_keys=False,
            allow_unicode=False,
        )
        self._atomic_write(self.config_path, document, mode=0o600)

    def _read_secrets(self) -> dict[str, str]:
        if not self.env_path.is_file():
            return {}
        result: dict[str, str] = {}
        try:
            lines = self.env_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ProviderConfigurationError(f"could not read model secrets: {exc}") from exc
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, raw_value = stripped.split("=", 1)
            try:
                values = shlex.split(raw_value, posix=True)
            except ValueError as exc:
                raise ProviderConfigurationError(f"model secrets file is invalid: {exc}") from exc
            if len(values) != 1:
                raise ProviderConfigurationError(
                    f"model secrets file is invalid: {name.strip()} must have one value"
                )
            result[name.strip()] = values[0]
        return result

    @staticmethod
    def _required_string(payload: dict, name: str) -> str:
        value = payload[name]
        if not isinstance(value, str) or not value.strip():
            raise TypeError(f"{name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _positive_int(payload: dict, name: str) -> int:
        value = payload[name]
        if isinstance(value, bool):
            raise TypeError(f"{name} must be a positive integer")
        parsed = int(value)
        if parsed < 1:
            raise ValueError(f"{name} must be a positive integer")
        return parsed

    @staticmethod
    def _atomic_write(path: Path, content: str, *, mode: int) -> None:
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.chmod(temp_path, mode)
            os.replace(temp_path, path)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink()


class OpenAICompatibleProvider:
    """Shared HTTP transport for Ollama and the OpenAI API."""

    def __init__(
        self,
        provider_id: str,
        model: str,
        api_key: str,
        base_url: str,
        timeout: int,
        *,
        configuration_source: str = "workspace",
    ):
        if provider_id not in PROVIDER_SPECS and provider_id != "openai-compatible":
            raise ProviderConfigurationError(f"unsupported model provider: {provider_id}")
        self.provider_id = provider_id
        self.model = model
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.configuration_source = configuration_source

    @property
    def journal_context(self) -> dict[str, str]:
        return {
            "model_provider": self.provider_id,
            "model": self.model,
            "endpoint": self.base_url,
            "configuration_source": self.configuration_source,
        }

    def list_models(self) -> list[str]:
        body = self._request_json("/models")
        try:
            data = body["data"]
            if not isinstance(data, list):
                raise TypeError("data must be an array")
            model_ids = [item["id"] for item in data]
            if not all(isinstance(model_id, str) for model_id in model_ids):
                raise TypeError("model id must be a string")
        except (KeyError, TypeError) as exc:
            raise ModelClientError(f"models response is invalid: {exc}") from exc
        return model_ids

    def validate(self) -> None:
        models = self.list_models()
        if self.model not in models:
            raise ModelClientError(f"configured model is unavailable: {self.model}")
        response = self.complete_json(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": "Return one JSON object with the boolean field ok set to true.",
                    },
                    {"role": "user", "content": "Confirm structured JSON output."},
                ]
            }
        )
        if response.get("ok") is not True:
            raise ModelClientError("model structured-output validation failed: expected ok=true")

    def complete_json(self, payload: dict, schema: dict | None = None) -> dict:
        response_format = (
            {
                "type": "json_schema",
                "json_schema": {"name": schema["title"], "strict": True, "schema": schema},
            }
            if schema is not None
            else {"type": "json_object"}
        )
        request_payload = {
            "model": self.model,
            **payload,
            "response_format": response_format,
            "temperature": 0.2,
        }
        body = self._request_json("/chat/completions", request_payload)
        try:
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("message content must be a string")
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelClientError(f"chat completion response is invalid: {exc}") from exc
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelClientError(
                f"model returned invalid JSON content at line {exc.lineno}, column {exc.colno}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ModelClientError("model JSON content must be an object")
        return decoded

    def _request_json(self, path: str, payload: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST" if payload is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ModelClientError(f"model request failed: HTTP {exc.code} from {url}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise ModelClientError(f"model endpoint is unreachable at {url}: {reason}") from exc
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise ModelClientError(
                f"model endpoint returned invalid JSON from {url} at "
                f"line {exc.lineno}, column {exc.colno}"
            ) from exc
        if not isinstance(body, dict):
            raise ModelClientError(f"model endpoint returned a non-object JSON response from {url}")
        return body


def create_provider(
    configuration: ModelConfiguration,
    config_store: ModelConfigStore,
    *,
    openai_api_key: str | None = None,
) -> OpenAICompatibleProvider:
    configuration.validate()
    if configuration.provider == "openai-api":
        api_key = openai_api_key or config_store.load_openai_api_key()
    else:
        api_key = "ollama"
    return OpenAICompatibleProvider(
        provider_id=configuration.provider,
        model=configuration.model,
        api_key=api_key,
        base_url=configuration.base_url,
        timeout=configuration.timeout,
        configuration_source="workspace",
    )
