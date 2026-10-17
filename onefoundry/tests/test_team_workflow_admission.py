"""Admission must describe every missing capability before an agent is queued."""

from uuid import uuid4

import pytest

from onefoundry.settings import Settings
from onefoundry.task_session_admission import profile_blockers


@pytest.mark.parametrize("tools", [["repository_file_v1"], ["repository_file_v1", "write_text_artifact_v1"]])
def test_team_repository_reads_honor_explicit_disable_and_keep_other_exclusions(tools: list[str]) -> None:
    profile = {
        "name": "Engineer",
        "kind": "builtin",
        "allowed_tools": tools,
        "instructions": "Review committed source",
        "model": "configured-model",
        "provider": "openai_codex_runtime",
        "auth_kind": "account_runtime",
        "catalog_valid": True,
        "credential_ready": True,
        "model_present": True,
    }
    inactive = Settings(
        db_password=str(uuid4()),
        local_source_root="/approved-repositories",
        team_repository_read_enabled=False,
    )
    assert profile_blockers(profile, inactive)
    approved = Settings(
        db_password=str(uuid4()),
        local_source_root="/approved-repositories",
        team_repository_read_enabled=True,
    )
    assert profile_blockers(profile, approved) == []
    profile["allowed_tools"] = tools + ["github_pr_read_v1"]
    assert profile_blockers(profile, approved), "source activation must not admit other legacy exclusions"


def test_unregistered_video_capabilities_are_not_a_text_success() -> None:
    profile = {
        "name": "Producer",
        "kind": "custom",
        "allowed_tools": ["browser_record", "video_edit"],
        "instructions": "Produce a playable video",
        "model": "configured-model",
        "provider": "openai_codex_runtime",
        "auth_kind": "account_runtime",
        "catalog_valid": True,
        "credential_ready": True,
        "model_present": True,
    }
    blockers = profile_blockers(profile, Settings(db_password=str(uuid4())))
    assert any("browser_record" in item and "video_edit" in item for item in blockers)
    assert not any("API key" in item for item in blockers)


@pytest.mark.parametrize(
    "tool_id", ["repository_tree_v1", "repository_file_range_v1", "coding_artifact_text_v1"]
)
def test_team_maintenance_reads_use_existing_gate(tool_id: str) -> None:
    from onefoundry.task_session_tool_readiness import team_tool_readiness

    disabled = Settings(db_password=str(uuid4()), team_repository_read_enabled=False)
    enabled = Settings(db_password=str(uuid4()), team_repository_read_enabled=True)
    assert team_tool_readiness([tool_id], role_name="QA", role_kind="builtin", settings=disabled)[0] is False
    assert team_tool_readiness([tool_id], role_name="QA", role_kind="builtin", settings=enabled) == (
        True,
        None,
    )


def test_static_browser_uses_own_gate_and_role_scope() -> None:
    from onefoundry.task_session_tool_readiness import team_tool_readiness

    disabled = Settings(db_password=str(uuid4()), browser_static_enabled=False)
    assert not team_tool_readiness(
        ["browser_static_inspect_v1"], role_name="QA", role_kind="builtin", settings=disabled
    )[0]


def test_team_account_route_does_not_silently_fall_back_to_paid_api() -> None:
    profile = {
        "name": "Researcher",
        "kind": "builtin",
        "allowed_tools": [],
        "instructions": "Review supplied facts",
        "model": "configured-model",
        "provider": "openai_codex_runtime",
        "auth_kind": "account_runtime",
        "catalog_valid": True,
        "credential_ready": True,
        "model_present": True,
    }
    assert profile_blockers(profile, Settings(db_password=str(uuid4()))) == []
    profile["credential_ready"] = False
    assert profile_blockers(profile, Settings(db_password=str(uuid4()))) == [
        "The assigned model account is not connected."
    ]


def test_grok_account_admitted_only_with_connected_catalog_and_allowed_tools() -> None:
    profile = {
        "name": "Sales",
        "kind": "builtin",
        "allowed_tools": [],
        "instructions": "Write a draft",
        "model": "configured-model",
        "provider": "xai",
        "auth_kind": "account_runtime",
        "catalog_valid": True,
        "credential_ready": True,
        "model_present": True,
    }
    blockers = profile_blockers(profile, Settings(db_password=str(uuid4())))
    assert blockers == []
    profile["credential_ready"] = False
    assert profile_blockers(profile, Settings(db_password=str(uuid4()))) == [
        "The assigned model account is not connected."
    ]
    profile["credential_ready"] = True
    profile["allowed_tools"] = ["public_page_fetch_v1"]
    assert any(
        "unavailable to this" in item
        for item in profile_blockers(profile, Settings(db_password=str(uuid4())))
    )
