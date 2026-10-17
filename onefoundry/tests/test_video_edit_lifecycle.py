"""Settings and readiness distinguish configured editing from unavailable capabilities."""

import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from onefoundry.input_file_bytes import InputFileInvalid
from onefoundry.live_tool_readiness import live_tool_readiness
from onefoundry.main import create_app
from onefoundry.settings import Settings
from runner import video_edit_worker


def test_enabled_missing_storage_fails_at_app_owner(tmp_path: Path) -> None:
    settings = Settings(
        db_password="test-only",
        video_edit_enabled=True,
        video_edit_image="sha256:" + "a" * 64,
        video_edit_asset_root=str(tmp_path / "absent"),
    )
    with pytest.raises(InputFileInvalid, match="unavailable"):
        create_app(settings)


def test_disabled_edit_tool_is_not_live_ready() -> None:
    ready, reason = live_tool_readiness(
        ["edit_video_v1"],
        provider="openai",
        api_key_present=True,
        role_name="Producer",
        role_kind="custom",
        has_visuals=False,
        settings=Settings(db_password="test-only"),
    )
    assert not ready and reason and "disabled" in reason


def test_once_cli_claims_selected_project_without_global_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = uuid4()
    claims: list[UUID | None] = []
    monkeypatch.setenv("DB_PASSWORD", "test-only")

    class FakeWorker:
        def __init__(self, _gateway: Any) -> None:
            pass

        def process_one(self, selected: UUID | None = None) -> None:
            claims.append(selected)

        def reconcile_expired(self) -> None:
            pytest.fail("the scoped once pilot must not reconcile unrelated projects")

    monkeypatch.setattr(sys, "argv", ["video_edit_worker", "--once", "--project", str(project)])
    monkeypatch.setattr(video_edit_worker, "create_engine", lambda _url: object())
    monkeypatch.setattr(video_edit_worker, "VideoEditWorker", FakeWorker)
    video_edit_worker.main()
    assert claims == [project]


def test_once_cli_rejects_malformed_project_before_gateway_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["video_edit_worker", "--once", "--project", "invalid"])
    monkeypatch.setattr(
        video_edit_worker, "create_engine", lambda _url: pytest.fail("invalid project reached the gateway")
    )
    with pytest.raises(SystemExit) as error:
        video_edit_worker.main()
    assert error.value.code == 2
