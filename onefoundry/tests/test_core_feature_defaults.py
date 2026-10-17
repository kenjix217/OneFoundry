"""Core functionality defaults on without bypassing explicit operator settings."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from onefoundry.repository_code_contract import RepositoryCodePolicy
from onefoundry.settings import Settings


def test_repository_request_budget_default_and_explicit_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPOSITORY_CODE_POLICY", raising=False)
    assert Settings(db_password="fixture-only").repository_code_policy.max_provider_requests == 80
    monkeypatch.setenv("REPOSITORY_CODE_POLICY", '{"max_provider_requests": 20}')
    assert Settings(db_password="fixture-only").repository_code_policy.max_provider_requests == 20
    assert (
        Settings(
            db_password="fixture-only", repository_code_policy=RepositoryCodePolicy(max_provider_requests=100)
        ).repository_code_policy.max_provider_requests
        == 100
    )
    for invalid in (0, 101, "invalid"):
        monkeypatch.setenv("REPOSITORY_CODE_POLICY", json.dumps({"max_provider_requests": invalid}))
        with pytest.raises(ValidationError):
            Settings(db_password="fixture-only")


@pytest.mark.parametrize("name", ["team_worker_enabled", "public_web_enabled", "media_capture_enabled"])
def test_core_features_default_on_and_preserve_override_precedence(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv(name.upper(), raising=False)
    assert getattr(Settings(db_password="fixture-only"), name) is True
    monkeypatch.setenv(name.upper(), "false")
    assert getattr(Settings(db_password="fixture-only"), name) is False
    overrides: dict[str, Any] = {"db_password": "fixture-only", name: True}
    assert getattr(Settings(**overrides), name) is True
    monkeypatch.setenv(name.upper(), "true")
    assert getattr(Settings(db_password="fixture-only"), name) is True
    for invalid in ("", "sometimes"):
        monkeypatch.setenv(name.upper(), invalid)
        with pytest.raises(ValidationError):
            Settings(db_password="fixture-only")
