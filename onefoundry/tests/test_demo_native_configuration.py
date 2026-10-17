"""Approved demo capabilities use registered tools and preserve admission checks."""

import pytest

from onefoundry.settings import Settings
from onefoundry.task_session_admission import profile_blockers


@pytest.mark.parametrize(
    "name,kind,tools",
    [
        ("Chief of Staff", "custom", ["write_text_artifact_v1"]),
        ("Researcher", "builtin", ["write_text_artifact_v1"]),
        ("Engineer", "builtin", ["write_text_artifact_v1"]),
        ("QA", "builtin", ["write_text_artifact_v1"]),
        ("Demo Video Producer", "custom", ["write_text_artifact_v1", "request_video_capture_v1"]),
    ],
)
def test_minimal_demo_profiles_are_admitted(name: str, kind: str, tools: list[str]) -> None:
    profile = {
        "name": name,
        "kind": kind,
        "allowed_tools": tools,
        "instructions": "Public-record demo task",
        "model": "configured-model",
        "provider": "openai_codex_runtime",
        "auth_kind": "account_runtime",
        "catalog_valid": True,
        "credential_ready": True,
        "model_present": True,
    }
    settings = Settings(db_password="test-db-value", team_worker_enabled=True, media_capture_enabled=True)
    assert profile_blockers(profile, settings) == []
    profile["credential_ready"] = False
    assert profile_blockers(profile, settings) == ["The assigned model account is not connected."]
