"""An approved PR read binds one role run and one saved GitHub source."""

import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.tool_gateway import NotFound, ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}
PIN = "a" * 40
HEAD = "b" * 40
TOKEN = "fixture-credential-for-pr-read"


def _post(client: TestClient, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    response = client.post(path, json=payload or {}, headers=ORIGIN)
    assert response.status_code in (200, 201), response.text
    return response.json()  # type: ignore[no-any-return]


def _setup(client: TestClient, gateway: ToolGateway) -> tuple[UUID, UUID, UUID, UUID]:
    project = _post(client, "/api/projects", {"name": "Approved PR read"})
    project_id = UUID(str(project["id"]))
    path = f"/api/projects/{project_id}"
    profile = next(item for item in client.get(f"{path}/profiles").json() if item["name"] == "Researcher")
    profile_id = UUID(profile["id"])
    response = client.patch(
        f"{path}/profiles/{profile_id}", json={"allowed_tools": ["github_pr_read_v1"]}, headers=ORIGIN
    )
    assert response.status_code == 200, response.text
    order = _post(
        client,
        f"{path}/work-orders",
        {
            "title": "Inspect saved PR",
            "instructions": "Read PR evidence",
            "profile_id": str(profile_id),
        },
    )
    _post(client, f"{path}/work-orders/{order['id']}/ready")
    prepared = gateway.start_synthetic_text(project_id, UUID(str(order["id"])))
    run_id = UUID(str(prepared["run_id"]))
    account = _post(client, f"{path}/github/accounts", {"label": "PR fixture", "token": TOKEN})
    with patch("onefoundry.source_store.resolve_revision", return_value=("main", PIN)):
        source = _post(
            client,
            f"{path}/sources/github",
            {
                "account_id": str(account["id"]),
                "owner": "sample",
                "repo": "project",
                "branch": "main",
            },
        )
    return project_id, run_id, profile_id, UUID(str(source["id"]))


def _proposal(client: TestClient, project_id: UUID, run_id: UUID, source_id: UUID) -> UUID:
    result = _post(
        client,
        f"/api/projects/{project_id}/runs/{run_id}/github-pr-read-approvals",
        {"source_id": str(source_id), "number": 7, "idempotency_key": str(uuid4())},
    )
    assert result["status"] == "pending"
    return UUID(str(result["approval_id"]))


def _result() -> dict[str, object]:
    return {
        "number": 7,
        "title": "Fix issue",
        "body": "Reviewed change",
        "state": "open",
        "head_sha": HEAD,
        "base_sha": PIN,
        "files_truncated": False,
        "files": [
            {
                "path": "src/main.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
                "patch_excerpt": "@@ -1 +1 @@\n+fixed\n",
                "patch_truncated": False,
            }
        ],
    }


def test_pr_read_requires_exact_approval_and_records_one_use(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project_id, run_id, profile_id, source_id = _setup(client, gateway)
            other = _post(client, "/api/projects", {"name": "Other PR project"})
            approval_id = _proposal(client, project_id, run_id, source_id)
            with patch("onefoundry.role_github_pr.read_pull_request") as network:
                with pytest.raises((ToolDenied, NotFound)):
                    gateway.invoke_role_tool(
                        project_id,
                        run_id,
                        profile_id,
                        "pending",
                        "github_pr_read_v1",
                        {"approval_id": str(approval_id)},
                        tool_version=1,
                    )
                with pytest.raises((ToolDenied, NotFound)):
                    gateway.invoke_role_tool(
                        UUID(str(other["id"])),
                        run_id,
                        profile_id,
                        "other",
                        "github_pr_read_v1",
                        {"approval_id": str(approval_id)},
                        tool_version=1,
                    )
                network.assert_not_called()
            _post(
                client,
                f"/api/projects/{project_id}/approvals/{approval_id}/decision",
                {"decision": "approved"},
            )
            with patch("onefoundry.role_github_pr.read_pull_request", return_value=_result()) as network:
                receipt = gateway.invoke_role_tool(
                    project_id,
                    run_id,
                    profile_id,
                    "approved-1",
                    "github_pr_read_v1",
                    {"approval_id": str(approval_id)},
                    tool_version=1,
                )
                assert receipt["status"] == "done"
                assert receipt["result"] == _result()
                network.assert_called_once()
                assert network.call_args.args[0] == TOKEN
                assert network.call_args.args[5:7] == (PIN, "main")
                with pytest.raises(ToolDenied, match="already used"):
                    gateway.invoke_role_tool(
                        project_id,
                        run_id,
                        profile_id,
                        "approved-2",
                        "github_pr_read_v1",
                        {"approval_id": str(approval_id)},
                        tool_version=1,
                    )
                network.assert_called_once()
            with engine.connect() as connection:
                rows = (
                    connection.execute(
                        text(
                            "SELECT event_type,payload FROM events WHERE project_id=:project_id "
                            "AND run_id=:run_id AND event_type IN "
                            "('github_pr_read_proposed','github_pr_read_started','role_tool_finished')"
                        ),
                        {"project_id": project_id, "run_id": run_id},
                    )
                    .mappings()
                    .all()
                )
            events = [dict(row) for row in rows]
            assert {row["event_type"] for row in events} == {
                "github_pr_read_proposed",
                "github_pr_read_started",
                "role_tool_finished",
            }
            encoded = json.dumps(events)
            assert TOKEN not in encoded
            assert "Reviewed change" not in encoded
            assert "@@ -1" not in encoded
    finally:
        engine.dispose()


def test_pr_read_denies_source_drift_and_revoked_profile(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project_id, run_id, profile_id, source_id = _setup(client, gateway)
            approval_id = _proposal(client, project_id, run_id, source_id)
            _post(
                client,
                f"/api/projects/{project_id}/approvals/{approval_id}/decision",
                {"decision": "approved"},
            )
            response = client.delete(f"/api/projects/{project_id}/sources/{source_id}", headers=ORIGIN)
            assert response.status_code == 204
            with patch("onefoundry.role_github_pr.read_pull_request") as network:
                with pytest.raises(ToolDenied, match="source changed"):
                    gateway.invoke_role_tool(
                        project_id,
                        run_id,
                        profile_id,
                        "drift",
                        "github_pr_read_v1",
                        {"approval_id": str(approval_id)},
                        tool_version=1,
                    )
                network.assert_not_called()
            response = client.patch(
                f"/api/projects/{project_id}/profiles/{profile_id}",
                json={"allowed_tools": []},
                headers=ORIGIN,
            )
            assert response.status_code == 200
            assert gateway.available_role_tools(project_id, run_id, profile_id) == []
    finally:
        engine.dispose()
