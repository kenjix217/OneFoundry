"""Fixed acceptance for team admission of already granted maintenance evidence."""

from uuid import uuid4

import pytest

from onefoundry.live_tool_readiness import live_tool_readiness
from onefoundry.settings import Settings
from onefoundry.task_session_tool_readiness import team_tool_readiness


@pytest.mark.parametrize("enabled", [False, True])
def test_team_artifact_reader_obeys_explicit_repository_policy(enabled: bool) -> None:
    settings = Settings(db_password=str(uuid4()), team_repository_read_enabled=enabled)
    active, reason = team_tool_readiness(
        ["coding_evidence_v1", "coding_artifact_text_v1"],
        role_name="Engineer",
        role_kind="builtin",
        settings=settings,
    )
    assert active is enabled
    assert (reason is None) is enabled


@pytest.mark.parametrize("enabled", [False, True])
def test_team_browser_reader_obeys_its_existing_activation(enabled: bool) -> None:
    settings = Settings(
        db_password=str(uuid4()),
        browser_static_enabled=enabled,
        browser_static_image_id="sha256:" + "a" * 64,
    )
    active, reason = team_tool_readiness(
        ["browser_static_inspect_v1"],
        role_name="QA",
        role_kind="builtin",
        settings=settings,
    )
    assert active is enabled
    assert (reason is None) is enabled


def test_combined_grants_start_team_without_weakening_legacy_live_exclusions() -> None:
    settings = Settings(
        db_password=str(uuid4()),
        team_repository_read_enabled=True,
        browser_static_enabled=True,
        browser_static_image_id="sha256:" + "a" * 64,
    )
    tools = [
        "repository_file_v1",
        "coding_evidence_v1",
        "coding_artifact_text_v1",
        "browser_static_inspect_v1",
    ]
    assert team_tool_readiness(tools, role_name="QA", role_kind="builtin", settings=settings) == (True, None)
    for tool in ("github_pr_read_v1", "github_pr_comment_v1", "unregistered_fixture_tool"):
        assert team_tool_readiness([tool], role_name="QA", role_kind="builtin", settings=settings)[1]
    for tool in ("repository_file_v1", "coding_artifact_text_v1", "browser_static_inspect_v1"):
        assert live_tool_readiness(
            [tool],
            provider="openai",
            api_key_present=True,
            role_name="QA",
            role_kind="builtin",
            has_visuals=False,
            settings=settings,
        )[1]
