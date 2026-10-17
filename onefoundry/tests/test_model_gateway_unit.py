"""Provider discovery and credential boundary tests without external network."""

import io
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from onefoundry.model_gateway import AccountCreate, require_valid_key, validate_local_endpoint
from onefoundry.model_providers import DiscoveryError, discover_models
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.settings import Settings


class FakeResponse:
    status = 200

    def __init__(self, body: dict[str, Any]) -> None:
        self.stream = io.BytesIO(json.dumps(body).encode())

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.stream.read(size)


def settings() -> Settings:
    return Settings(db_password="test-value")


@pytest.mark.parametrize(
    "provider,body,expected",
    [
        ("openai", {"data": [{"id": "gpt-test", "object": "model"}]}, "gpt-test"),
        ("grok", {"data": [{"id": "grok-test", "object": "model"}]}, "grok-test"),
        (
            "anthropic",
            {"data": [{"id": "claude-test", "display_name": "Claude Test"}], "has_more": False},
            "claude-test",
        ),
        (
            "gemini",
            {"models": [{"name": "models/gemini-test", "supportedGenerationMethods": ["generateContent"]}]},
            "models/gemini-test",
        ),
    ],
)
def test_discovery_uses_fixed_https_and_header_key(
    provider: str, body: dict[str, Any], expected: str
) -> None:
    class FakeOpener:
        def open(self, request: Any, timeout: int) -> FakeResponse:
            assert request.full_url.startswith("https://")
            assert "sensitive-test-value" not in request.full_url
            assert timeout == 5
            assert any("sensitive-test-value" in value for value in request.headers.values())
            return FakeResponse(body)

    with patch("onefoundry.model_providers.build_opener", return_value=FakeOpener()):
        found = discover_models(provider, "sensitive-test-value", settings())
    assert [model.model_id for model in found] == [expected]


def test_discovery_rejects_invalid_and_incomplete_pages() -> None:
    class FakeOpener:
        def open(self, request: Any, timeout: int) -> FakeResponse:
            return FakeResponse({"data": [{"id": "claude-test"}], "has_more": True})

    with patch("onefoundry.model_providers.build_opener", return_value=FakeOpener()):
        with pytest.raises(DiscoveryError, match="pagination"):
            discover_models("anthropic", "sensitive-test-value", settings())


def test_ollama_discovery_uses_keyless_local_endpoint() -> None:
    class FakeOpener:
        def open(self, request: Any, timeout: int) -> FakeResponse:
            assert request.full_url == "http://ollama:11434/api/tags"
            assert request.headers == {}
            assert timeout == 5
            return FakeResponse({"models": [{"name": "qwen-test:latest"}]})

    with patch("onefoundry.model_providers.build_opener", return_value=FakeOpener()):
        found = discover_models("ollama", "", settings(), "http://ollama:11434")
    assert [model.model_id for model in found] == ["qwen-test:latest"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://ollama:11434",
        "http://localhost:11434",
        "http://127.0.0.1:11434",
    ],
)
def test_local_endpoint_allows_only_expected_origins(endpoint: str) -> None:
    assert validate_local_endpoint(endpoint) == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://ollama:11434",
        "http://other:11434",
        "http://169.254.169.254:80",
        "http://ollama:11434/path",
        "http://user:pass@ollama:11434",
        "http://ollama:11434?x=1",
        "http://ollama",
        "file:///etc/passwd",
        "http://ollama:11434/#fragment",
    ],
)
def test_local_endpoint_rejects_egress_and_url_components(endpoint: str) -> None:
    with pytest.raises(ValueError, match="local HTTP origin"):
        validate_local_endpoint(endpoint)


def test_secret_store_permissions_and_symlink_refusal(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    store = ModelSecretStore(str(root))
    account_id = uuid4()
    store.write(account_id, "sensitive-test-value")
    assert store.read(account_id) == "sensitive-test-value"
    assert (root / str(account_id)).stat().st_mode & 0o777 == 0o600
    (root / str(account_id)).unlink()
    (root / str(account_id)).symlink_to(tmp_path / "other")
    assert store.present(account_id) is False
    with pytest.raises(SecretStoreError, match="permissions"):
        store.read(account_id)
    (root / str(account_id)).unlink()
    os.chmod(root, 0o755)  # noqa: S103 - deliberately test insecure permissions
    with pytest.raises(SecretStoreError, match="permissions"):
        store.write(account_id, "sensitive-test-value")


def test_orphan_cleanup_only_removes_uuid_files(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    store = ModelSecretStore(str(root))
    retained = uuid4()
    orphan = uuid4()
    store.write(retained, "retained-test-value")
    store.write(orphan, "orphan-test-value")
    (root / "unrelated-note").write_text("leave")
    store.remove_orphans({retained})
    assert store.read(retained) == "retained-test-value"
    assert not (root / str(orphan)).exists()
    assert (root / "unrelated-note").exists()


def test_key_validation_redacts_input() -> None:
    attempted = "sensitive test value"
    data = AccountCreate.model_validate({"provider": "openai", "label": "Account", "api_key": attempted})
    assert data.api_key is not None
    with pytest.raises(HTTPException) as error:
        require_valid_key(data.api_key)
    assert attempted not in str(error.value)


def test_model_settings_validate_url_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = settings()
    assert defaults.model_secret_root == "/model-secrets"
    assert defaults.model_discovery_max_pages == 5
    assert defaults.live_text_max_prompt_bytes == 96000
    assert defaults.live_text_max_output_tokens == 512
    assert defaults.live_text_max_output_bytes == 16000
    assert defaults.live_text_timeout_seconds == 30
    assert defaults.live_text_openai_base_url == "https://api.openai.com/v1"
    assert defaults.live_text_grok_base_url == "https://api.x.ai/v1"
    monkeypatch.setenv("MODEL_SECRET_ROOT", "/var/lib/onefoundry/test-secrets")
    monkeypatch.setenv("MODEL_DISCOVERY_MAX_PAGES", "3")
    assert settings().model_secret_root == "/var/lib/onefoundry/test-secrets"
    assert settings().model_discovery_max_pages == 3
    assert Settings(db_password="test-value", model_discovery_max_pages=2).model_discovery_max_pages == 2
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", model_openai_list_url="http://localhost/models")
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", model_discovery_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", model_secret_root="relative/path")
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", model_discovery_max_pages=11)
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", live_text_max_prompt_bytes=0)
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", live_text_max_output_tokens=0)
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", live_text_max_output_bytes=20000)
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", live_text_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", live_text_openai_base_url="http://localhost:11434/v1")
    with pytest.raises(ValidationError):
        Settings(db_password="test-value", live_text_grok_base_url="https://example.com/v1")
