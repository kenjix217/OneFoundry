"""Saved profile configuration versus observed run admission on PostgreSQL."""

import os
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient

from onefoundry.inference import TextResult, TransportError
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}


def _client(tmp_path: Path) -> TestClient:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    input_root = tmp_path / "inputs"
    input_root.mkdir(mode=0o700)
    return TestClient(
        create_app(
            Settings(
                db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                db_password=os.environ["APP_DB_PASSWORD"],
                app_public_port=8765,
                model_secret_root=str(secret_root),
                input_file_root=str(input_root),
            )
        ),
        base_url="http://localhost:8765",
    )


def _profile(client: TestClient, name: str) -> tuple[str, str]:
    project = client.post("/api/projects", json={"name": name}, headers=ORIGIN).json()
    profile = next(
        item
        for item in client.get(f"/api/projects/{project['id']}/profiles").json()
        if item["name"] == "Researcher"
    )
    return project["id"], profile["id"]


def _model(client: TestClient, project_id: str, profile_id: str) -> None:
    base = f"/api/projects/{project_id}/model"
    account = client.post(
        f"{base}/accounts",
        json={"provider": "openai", "label": "Fixture", "api_key": "test-only-api-key"},
        headers=ORIGIN,
    ).json()
    with patch(
        "onefoundry.model_gateway.discover_models",
        return_value=[DiscoveredModel("gpt-test", "Fixture", "text")],
    ):
        assert client.post(f"{base}/accounts/{account['id']}/refresh", headers=ORIGIN).status_code == 200
    configured = client.patch(
        f"/api/projects/{project_id}/profiles/{profile_id}",
        json={
            "model": "gpt-test",
            "model_account_id": account["id"],
            "allowed_tools": ["current_work_order"],
        },
        headers=ORIGIN,
    )
    assert configured.status_code == 200, configured.text


def _order(client: TestClient, project_id: str, profile_id: str) -> str:
    base = f"/api/projects/{project_id}/work-orders"
    created = client.post(
        base,
        json={"title": "Readback fixture", "instructions": "Write a short draft", "profile_id": profile_id},
        headers=ORIGIN,
    )
    assert created.status_code == 201
    order_id = created.json()["id"]
    assert client.post(f"{base}/{order_id}/ready", headers=ORIGIN).status_code == 200
    return str(order_id)


def test_no_run_and_foreign_project_are_scoped(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, profile_id = _profile(client, "Capability A")
        other_id, _ = _profile(client, "Capability B")
        path = f"/api/projects/{project_id}/profiles/{profile_id}/onefoundry-capability"
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "project_id": project_id,
            "profile_id": profile_id,
            "configured_model_id": None,
            "configured_tool_ids": [],
            "last_run": None,
        }
        assert client.get(path.replace(project_id, other_id)).status_code == 404
        assert client.get(path.replace(profile_id, str(UUID(int=1)))).status_code == 404


def test_inactive_active_and_unknown_observations(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, profile_id = _profile(client, "Capability observations")
        _model(client, project_id, profile_id)
        path = f"/api/projects/{project_id}/profiles/{profile_id}/onefoundry-capability"
        configured = client.get(path).json()
        assert configured["configured_model_id"] == "gpt-test"
        assert configured["configured_tool_ids"] == ["current_work_order"]
        assert configured["last_run"] is None

        # A manual run has no live-text admission observation.
        manual_id = _order(client, project_id, profile_id)
        manual = client.post(f"/api/projects/{project_id}/work-orders/{manual_id}/runs", headers=ORIGIN)
        assert manual.status_code == 201
        assert client.get(path).json()["last_run"] == {
            "id": manual.json()["id"],
            "status": "running",
            "tools_active": None,
        }

        # An unadmitted tool fails before a model call or run is recorded.
        inactive_id = _order(client, project_id, profile_id)
        with (
            patch("onefoundry.live_text_store._provider_transport") as transport,
            patch("onefoundry.live_tool_sdk.run_live_tool_sdk") as sdk,
        ):
            # Exclude the SDK by configuring a non-admitted tool for this attempt.
            update = client.patch(
                f"/api/projects/{project_id}/profiles/{profile_id}",
                json={"allowed_tools": ["repository_file_v1"]},
                headers=ORIGIN,
            )
            assert update.status_code == 200
            inactive = client.post(
                f"/api/projects/{project_id}/work-orders/{inactive_id}/live-text-run", headers=ORIGIN
            )
            transport.assert_not_called()
            sdk.assert_not_called()
        assert inactive.status_code == 409, inactive.text
        assert client.get(path).json()["last_run"]["id"] == manual.json()["id"]

        blocked = next(
            item
            for item in client.get(f"/api/projects/{project_id}/work-orders").json()
            if item["id"] == inactive_id
        )
        assert blocked["status"] == "blocked"
        unblocked = client.post(
            f"/api/projects/{project_id}/work-orders/{inactive_id}/unblock",
            json={"expected_version": blocked["version"]},
            headers=ORIGIN,
        )
        assert unblocked.status_code == 200 and unblocked.json()["status"] == "ready"

        # A profile requesting no tools may still make a clearly text-only call.
        update = client.patch(
            f"/api/projects/{project_id}/profiles/{profile_id}",
            json={"allowed_tools": []},
            headers=ORIGIN,
        )
        assert update.status_code == 200
        with patch("onefoundry.live_text_store._provider_transport") as transport:
            transport.return_value.generate.return_value = TextResult("Draft", "inactive", 2, 2, "completed")
            inactive = client.post(
                f"/api/projects/{project_id}/work-orders/{inactive_id}/live-text-run", headers=ORIGIN
            )
        assert inactive.status_code == 201, inactive.text
        assert client.get(path).json()["last_run"] == {
            "id": inactive.json()["run"]["id"],
            "status": "done",
            "tools_active": False,
        }

        active_id = _order(client, project_id, profile_id)
        update = client.patch(
            f"/api/projects/{project_id}/profiles/{profile_id}",
            json={"allowed_tools": ["current_work_order"]},
            headers=ORIGIN,
        )
        assert update.status_code == 200
        with patch("onefoundry.live_tool_sdk.run_live_tool_sdk") as sdk:
            sdk.return_value = TextResult("Draft", "active", 2, 2, "completed")
            active = client.post(
                f"/api/projects/{project_id}/work-orders/{active_id}/live-text-run", headers=ORIGIN
            )
        assert active.status_code == 201, active.text
        assert client.get(path).json()["last_run"] == {
            "id": active.json()["run"]["id"],
            "status": "done",
            "tools_active": True,
        }

        unknown_id = _order(client, project_id, profile_id)
        with patch("onefoundry.live_tool_sdk.run_live_tool_sdk", side_effect=TransportError("uncertain")):
            unknown = client.post(
                f"/api/projects/{project_id}/work-orders/{unknown_id}/live-text-run", headers=ORIGIN
            )
        assert unknown.status_code == 503, unknown.text
        assert client.get(path).json()["last_run"] == {
            "id": unknown.json()["run_id"],
            "status": "unknown",
            "tools_active": True,
        }
