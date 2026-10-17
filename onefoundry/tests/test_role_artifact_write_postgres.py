"""A role write has a committed intent, scoped bytes, and a terminal receipt."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.artifact_workspace import ArtifactWorkspace
from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied, ToolInputError, ToolOutcomeUnknown
from onefoundry.settings import Settings
from onefoundry.tool_gateway import NotFound, ToolGateway
from tests.test_role_tool_admission_postgres import _run, _tool_events

TOOL = "write_text_artifact_v1"
ORIGIN = {"origin": "http://localhost:8765"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = tmp_path / "private-artifacts"
    root.mkdir(mode=0o700)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        app_public_port=8765,
        artifact_workspace_root=str(root),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as test_client:
        yield test_client


@pytest.fixture
def gateway(settings: Settings) -> Iterator[ToolGateway]:
    engine = create_engine(settings.database_url)
    try:
        yield ToolGateway(engine, "operator", live_text_settings=settings)
    finally:
        engine.dispose()


def _write(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    *,
    kind: str = "markdown",
    content: str = "# Safe note\n",
) -> dict[str, Any]:
    return gateway.invoke_role_tool(
        project_id,
        run_id,
        profile_id,
        call_id,
        TOOL,
        {"kind": kind, "content": content},
        tool_version=1,
    )


def _owner(gateway: ToolGateway, project_id: UUID, run_id: UUID, call_id: str) -> dict[str, Any]:
    with gateway.engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT id,project_id,run_id,actor_id,call_id,kind,mime_type,relative_path,"
                    "sha256,byte_size,status FROM workspace_artifacts "
                    "WHERE project_id=:project_id AND run_id=:run_id AND call_id=:call_id"
                ),
                {"project_id": project_id, "run_id": run_id, "call_id": call_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_write_publishes_scoped_metadata_and_hash_only_events(
    client: TestClient, gateway: ToolGateway, settings: Settings
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL])
    assert TOOL in {entry["id"] for entry in gateway.available_role_tools(project_id, run_id, profile_id)}
    receipt = _write(gateway, project_id, run_id, profile_id, "write-one")
    assert receipt["status"] == "done"
    result = receipt["result"]
    assert set(result) == {"artifact_id", "kind", "mime_type", "sha256", "byte_size"}
    assert result["kind"] == "markdown"
    assert result["mime_type"] == "text/markdown"
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert receipt["result_sha256"] == hashlib.sha256(canonical).hexdigest()
    owner = _owner(gateway, project_id, run_id, "write-one")
    assert owner["status"] == "ready"
    assert owner["actor_id"] == f"profile:{profile_id}"
    assert owner["id"] == UUID(result["artifact_id"])
    assert owner["sha256"] == result["sha256"]
    assert owner["byte_size"] == result["byte_size"]
    assert (
        ArtifactWorkspace(settings.artifact_workspace_root).read(
            project_id,
            run_id,
            owner["id"],
            relative_path=owner["relative_path"],
            expected_sha256=owner["sha256"],
            expected_byte_size=owner["byte_size"],
            kind=owner["kind"],
            mime_type=owner["mime_type"],
        )
        == b"# Safe note\n"
    )
    events = _tool_events(gateway, project_id, run_id)
    assert [item["event_type"] for item in events] == ["role_tool_started", "role_tool_finished"]
    assert events[1]["payload"]["status"] == "done"
    assert events[1]["payload"]["result_sha256"] == receipt["result_sha256"]
    assert events[1]["payload"]["content_sha256"] == result["sha256"]
    assert "# Safe note" not in json.dumps(events)
    assert owner["relative_path"] not in json.dumps(receipt)
    assert owner["relative_path"] not in json.dumps(events)
    listing = client.get(f"/api/projects/{project_id}/runs/{run_id}/workspace-artifacts")
    assert listing.status_code == 200, listing.text
    assert len(listing.json()) == 1
    assert listing.json()[0]["id"] == result["artifact_id"]
    assert "relative_path" not in listing.json()[0]
    saved = client.get(
        f"/api/projects/{project_id}/runs/{run_id}/workspace-artifacts/{result['artifact_id']}"
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["content"] == "# Safe note\n"
    assert "relative_path" not in saved.json()
    other = client.get(
        f"/api/projects/{UUID(int=0)}/runs/{run_id}/workspace-artifacts/{result['artifact_id']}"
    )
    assert other.status_code == 404


def test_write_accepts_maximum_ascii_artifact(
    client: TestClient, gateway: ToolGateway, settings: Settings
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL])
    content = "x" * 65536
    receipt = _write(gateway, project_id, run_id, profile_id, "maximum", kind="text", content=content)
    assert receipt["result"]["byte_size"] == 65536
    owner = _owner(gateway, project_id, run_id, "maximum")
    assert (
        ArtifactWorkspace(settings.artifact_workspace_root).read(
            project_id,
            run_id,
            owner["id"],
            relative_path=owner["relative_path"],
            expected_sha256=owner["sha256"],
            expected_byte_size=owner["byte_size"],
            kind=owner["kind"],
            mime_type=owner["mime_type"],
        )
        == content.encode()
    )


def test_operator_readback_rejects_changed_bytes(
    client: TestClient, gateway: ToolGateway, settings: Settings
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL])
    receipt = _write(gateway, project_id, run_id, profile_id, "changed")
    owner = _owner(gateway, project_id, run_id, "changed")
    stored = Path(settings.artifact_workspace_root) / owner["relative_path"]
    stored.write_bytes(b"# Fake note\n")
    response = client.get(
        f"/api/projects/{project_id}/runs/{run_id}/workspace-artifacts/{receipt['result']['artifact_id']}"
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "artifact readback failed"


def test_write_rejects_invalid_or_sensitive_content_before_intent(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL])
    for call_id, kind, content in (
        ("blank", "text", " \n"),
        ("secret", "text", "password=letmein"),
        ("formula", "csv", "name,value\na,=NOW()\n"),
        ("long", "text", "x" * 65537),
        ("bad-kind", "html", "<p>unsafe</p>"),
    ):
        with pytest.raises(ToolInputError):
            _write(gateway, project_id, run_id, profile_id, call_id, kind=kind, content=content)
    assert _tool_events(gateway, project_id, run_id) == []
    with gateway.engine.connect() as connection:
        count = connection.scalar(
            text("SELECT count(*) FROM workspace_artifacts WHERE project_id=:project_id AND run_id=:run_id"),
            {"project_id": project_id, "run_id": run_id},
        )
    assert count == 0


def test_write_denies_revoked_cross_project_and_duplicate_calls(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL])
    _write(gateway, project_id, run_id, profile_id, "once")
    with pytest.raises(ToolDenied, match="already attempted"):
        _write(gateway, project_id, run_id, profile_id, "once", content="changed")
    with pytest.raises((ToolDenied, NotFound)):
        _write(gateway, UUID(int=0), run_id, profile_id, "foreign")
    response = client.patch(
        f"/api/projects/{project_id}/profiles/{profile_id}",
        json={"allowed_tools": []},
        headers=ORIGIN,
    )
    assert response.status_code == 200, response.text
    assert gateway.available_role_tools(project_id, run_id, profile_id) == []
    with pytest.raises(ToolDenied, match="not admitted"):
        _write(gateway, project_id, run_id, profile_id, "revoked")
    with gateway.engine.connect() as connection:
        calls: list[Any] = list(
            connection.execute(
                text(
                    "SELECT call_id FROM workspace_artifacts "
                    "WHERE project_id=:project_id AND run_id=:run_id ORDER BY call_id"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            .scalars()
            .all()
        )
    assert [str(call) for call in calls] == ["once"]


def test_write_failure_after_intent_is_unknown_and_never_retried(
    client: TestClient, gateway: ToolGateway, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL])
    workspace = ArtifactWorkspace(settings.artifact_workspace_root)
    original_write = workspace.write
    writes = 0

    def fail_after_write(*args: Any, **kwargs: Any) -> Any:
        nonlocal writes
        writes += 1
        original_write(*args, **kwargs)
        raise OSError("simulated uncertain storage response")

    monkeypatch.setattr(workspace, "write", fail_after_write)
    monkeypatch.setattr(gateway, "_artifact_workspace", lambda: workspace)
    with pytest.raises(ToolOutcomeUnknown):
        _write(gateway, project_id, run_id, profile_id, "uncertain")
    owner = _owner(gateway, project_id, run_id, "uncertain")
    assert owner["status"] == "unknown"
    assert writes == 1
    with pytest.raises((ToolOutcomeUnknown, ToolDenied)):
        _write(gateway, project_id, run_id, profile_id, "uncertain")
    assert writes == 1
    events = _tool_events(gateway, project_id, run_id)
    assert [item["event_type"] for item in events] == ["role_tool_started", "role_tool_finished"]
    assert events[1]["payload"]["status"] == "unknown"
    assert "Safe note" not in json.dumps(events)
