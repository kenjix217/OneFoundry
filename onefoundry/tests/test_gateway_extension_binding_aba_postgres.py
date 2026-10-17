"""A saved binding generation remains part of each admitted run's exact tool pin."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from onefoundry.gateway_extension_spool import publish_json, read_json
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.tool_gateway import ToolGateway
from tests.test_gateway_extension_remote_postgres import ORIGIN, _ready_extension
from tests.test_gateway_extension_worker_postgres import _python_run

pytest_plugins = ("tests.test_gateway_extension_remote_postgres",)


def _run(client: TestClient, gateway: ToolGateway, project_id: UUID, profile_id: UUID) -> UUID:
    base = f"/api/projects/{project_id}/work-orders"
    order = client.post(
        base,
        json={
            "title": "Check exact saved binding",
            "instructions": "Read the fixture",
            "profile_id": str(profile_id),
        },
        headers=ORIGIN,
    )
    assert order.status_code == 201, order.text
    ready = client.post(f"{base}/{order.json()['id']}/ready", json={}, headers=ORIGIN)
    assert ready.status_code == 200, ready.text
    return UUID(str(gateway.start_synthetic_text(project_id, UUID(order.json()["id"]))["run_id"]))


def test_binding_aba_invalidates_old_run_without_changing_connection_version(
    setup: tuple[TestClient, ToolGateway], remote: str
) -> None:
    client, gateway = setup
    project_id, _implicit_run, profile_id, tool_id = _ready_extension(client, gateway, remote)
    base = f"/api/projects/{project_id}/extensions"
    with gateway.engine.connect() as connection:
        package_id = connection.scalar(
            text("SELECT id FROM extension_packages WHERE project_id=:p AND package_key='remote-fixture'"),
            {"p": project_id},
        )
        first = (
            connection.execute(
                text(
                    "SELECT id,version FROM extension_connections WHERE project_id=:p AND package_id=:package"
                ),
                {"p": project_id, "package": package_id},
            )
            .mappings()
            .one()
        )
    assert package_id is not None
    binding_url = f"{base}/packages/{package_id}/bindings/api"
    selected = client.put(
        binding_url, json={"connection_id": str(first["id"]), "expected_version": 0}, headers=ORIGIN
    )
    assert selected.status_code == 200, selected.text
    assert selected.json()["version"] == 1
    old_run = _run(client, gateway, project_id, profile_id)
    assert tool_id in {item["id"] for item in gateway.available_role_tools(project_id, old_run, profile_id)}

    second = client.post(
        f"{base}/connections",
        json={
            "package_id": str(package_id),
            "template_id": "api",
            "label": "Other saved connection",
            "config": {"transport": "streamable_http", "endpoint": remote},
        },
        headers=ORIGIN,
    )
    assert second.status_code == 201, second.text
    enabled = client.patch(
        f"{base}/connections/{second.json()['id']}",
        json={"expected_version": 1, "enabled": True},
        headers=ORIGIN,
    )
    assert enabled.status_code == 200, enabled.text
    switched = client.put(
        binding_url, json={"connection_id": second.json()["id"], "expected_version": 1}, headers=ORIGIN
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["version"] == 2
    restored = client.put(
        binding_url, json={"connection_id": str(first["id"]), "expected_version": 2}, headers=ORIGIN
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["version"] == 3
    with gateway.engine.connect() as connection:
        unchanged_connection_version = connection.scalar(
            text("SELECT version FROM extension_connections WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": first["id"]},
        )
        old_pin = connection.scalar(
            text(
                "SELECT binding_version FROM extension_run_tools "
                "WHERE project_id=:p AND run_id=:run AND tool_id=:tool"
            ),
            {"p": project_id, "run": old_run, "tool": tool_id},
        )
    assert unchanged_connection_version == first["version"]
    assert old_pin == 1
    assert tool_id not in {
        item["id"] for item in gateway.available_role_tools(project_id, old_run, profile_id)
    }
    with pytest.raises(ToolDenied):
        gateway.invoke_role_tool(
            project_id, old_run, profile_id, "stale-aba", tool_id, {"term": "ok"}, tool_version=1
        )

    new_run = _run(client, gateway, project_id, profile_id)
    assert tool_id in {item["id"] for item in gateway.available_role_tools(project_id, new_run, profile_id)}
    with gateway.engine.connect() as connection:
        new_pin = connection.scalar(
            text(
                "SELECT binding_version FROM extension_run_tools "
                "WHERE project_id=:p AND run_id=:run AND tool_id=:tool"
            ),
            {"p": project_id, "run": new_run, "tool": tool_id},
        )
    assert new_pin == 3
    pending = gateway.invoke_role_tool(
        project_id, new_run, profile_id, "fresh-binding", tool_id, {"term": "ok"}, tool_version=1
    )
    assert pending["status"] == "approval_required"


def test_worker_image_change_invalidates_old_run_and_new_run_pins_current_image(
    setup: tuple[TestClient, ToolGateway], remote: str, tmp_path: Path
) -> None:
    client, gateway = setup
    project_id, old_run, profile_id, tool_id, spool = _python_run(client, gateway, remote, tmp_path)
    assert tool_id in {item["id"] for item in gateway.available_role_tools(project_id, old_run, profile_id)}
    settings = gateway.live_text_settings
    assert settings is not None
    previous_image = settings.extension_worker_image
    replacement_image = "sha256:" + "b" * 64
    settings.extension_worker_image = replacement_image
    heartbeat_path = spool / "heartbeat.json"
    heartbeat = read_json(heartbeat_path, 4096)
    heartbeat["image_id"] = replacement_image
    heartbeat["updated_at"] = datetime.now(UTC).isoformat()
    heartbeat_path.unlink()
    publish_json(heartbeat_path, heartbeat)
    assert tool_id not in {
        item["id"] for item in gateway.available_role_tools(project_id, old_run, profile_id)
    }
    with pytest.raises(ToolDenied):
        gateway.invoke_role_tool(project_id, old_run, profile_id, "old-image", tool_id, {}, tool_version=1)
    new_run = _run(client, gateway, project_id, profile_id)
    assert tool_id in {item["id"] for item in gateway.available_role_tools(project_id, new_run, profile_id)}
    with gateway.engine.connect() as connection:
        old_pin = connection.scalar(
            text(
                "SELECT worker_image FROM extension_run_tools "
                "WHERE project_id=:p AND run_id=:run AND tool_id=:tool"
            ),
            {"p": project_id, "run": old_run, "tool": tool_id},
        )
        new_pin = connection.scalar(
            text(
                "SELECT worker_image FROM extension_run_tools "
                "WHERE project_id=:p AND run_id=:run AND tool_id=:tool"
            ),
            {"p": project_id, "run": new_run, "tool": tool_id},
        )
    assert old_pin == previous_image
    assert new_pin == replacement_image
