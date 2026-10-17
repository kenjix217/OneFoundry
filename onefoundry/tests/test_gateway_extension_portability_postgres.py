"""Supported plugin formats remain inert and preserve native archive identity/resources."""

import base64
import io
import json
import os
import zipfile
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from onefoundry_sdk import ExtensionManifest, build_package, inspect_package
from sqlalchemy import create_engine, text

from onefoundry.gateway_extension_identity import canonical_tool_id
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.skill_store import checked_procedures, select_procedures
from onefoundry.tool_gateway import ToolGateway
from tests.test_gateway_extension_lifecycle_postgres import ORIGIN, _change_package, _project
from tests.test_gateway_extension_lifecycle_postgres import client as client


def test_native_archive_roundtrip_retains_exact_bytes_and_hash(client: TestClient) -> None:
    project = _project(client, "Native archive roundtrip")
    manifest = ExtensionManifest(id="native-roundtrip", version="1.0.0", name="Native roundtrip")
    archive = build_package(manifest, {"resources/notes.md": b"# Inert reference\nSynthetic content.\n"})
    base = f"/api/projects/{project}/extensions/packages"
    response = client.post(
        f"{base}/import",
        json={"format": "onefoundry", "archive_base64": base64.b64encode(archive).decode()},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    saved = response.json()["package"]
    assert saved["state"] == "draft"
    assert saved["sha256"] == inspect_package(archive).package_sha256
    exported = client.get(f"{base}/{saved['id']}/export?format=onefoundry")
    assert exported.status_code == 200
    assert exported.content == archive
    assert client.get(f"/api/projects/{project}/skills").json() == []


@pytest.mark.parametrize("format", ["codex-portable", "codex-legacy", "claude"])
def test_foreign_skill_roundtrip_activation_pin_and_revocation(client: TestClient, format: str) -> None:
    project = _project(client, f"Foreign archive {format}")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        metadata = ".claude-plugin/plugin.json" if format == "claude" else ".codex-plugin/plugin.json"
        if format == "codex-portable":
            metadata = "plugin.json"
        archive.writestr(
            metadata,
            json.dumps({"name": f"foreign-{format}", "version": "1.0.0", "description": "Synthetic import"}),
        )
        archive.writestr(
            "skills/brief/SKILL.md",
            "---\nname: Brief\ndescription: Draft a brief\n---\n# Brief\nRead resources/guide.md.\n",
        )
        archive.writestr("resources/guide.md", "# Guide\nSynthetic reference.\n")
        archive.writestr("LICENSE", "Synthetic license notice.\n")
        archive.writestr("NOTICE", "Original fixture attribution.\n")
    base = f"/api/projects/{project}/extensions/packages"
    response = client.post(
        f"{base}/import",
        json={"format": format, "archive_base64": base64.b64encode(output.getvalue()).decode()},
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    saved = response.json()["package"]
    assert saved["state"] == "draft"
    assert client.get(f"/api/projects/{project}/skills").json() == []
    exported = client.get(f"{base}/{saved['id']}/export?format={format}")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read("resources/guide.md") == b"# Guide\nSynthetic reference.\n"
        assert "portability-report.json" in archive.namelist()
        assert archive.read("LICENSE") == b"Synthetic license notice.\n"
        assert archive.read("NOTICE") == b"Original fixture attribution.\n"

    _change_package(client, project, saved, "activate")
    skills = client.get(f"/api/projects/{project}/skills").json()
    assert len(skills) == 1
    skill_id = skills[0]["id"]
    assert skills[0]["name"] == f"foreign-{format}: brief"
    profile = next(
        item
        for item in client.get(f"/api/projects/{project}/profiles").json()
        if item["name"] == "Researcher"
    )
    profile_id = UUID(profile["id"])
    bound = client.put(
        f"/api/projects/{project}/profiles/{profile_id}/skills/{skill_id}",
        json={"version": 1, "expected_binding_id": None},
        headers=ORIGIN,
    )
    assert bound.status_code == 200, bound.text
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        with engine.connect() as connection:
            selected = checked_procedures(connection, UUID(project), profile_id)
            assert len(selected) == 1
            assert selected[0]["skill_id"] == UUID(skill_id)
            assert (
                len(select_procedures(connection, UUID(project), profile_id, "Brief", "Draft a brief")) == 1
            )
        order = client.post(
            f"/api/projects/{project}/work-orders",
            json={"title": "Brief", "instructions": "Draft a brief", "profile_id": str(profile_id)},
            headers=ORIGIN,
        )
        assert order.status_code == 201, order.text
        ready = client.post(
            f"/api/projects/{project}/work-orders/{order.json()['id']}/ready",
            json={},
            headers=ORIGIN,
        )
        assert ready.status_code == 200, ready.text
        run = gateway.start_synthetic_text(UUID(project), UUID(order.json()["id"]))
        run_id = UUID(str(run["run_id"]))
        with engine.connect() as connection:
            snap = connection.execute(
                text("SELECT skill_id,version FROM run_skill_snapshots WHERE project_id=:p AND run_id=:run"),
                {"p": UUID(project), "run": run_id},
            ).one()
        assert snap.skill_id == UUID(skill_id)
        assert snap.version == 1
        undeclared_tool = canonical_tool_id(f"foreign-{format}", "brief")
        assert undeclared_tool not in {
            item["id"] for item in gateway.available_role_tools(UUID(project), run_id, profile_id)
        }
        _change_package(client, project, saved, "revoke")
        with engine.connect() as connection:
            assert checked_procedures(connection, UUID(project), profile_id) == []
            assert select_procedures(connection, UUID(project), profile_id, "Brief", "Draft a brief") == []
            persisted = connection.scalar(
                text("SELECT skill_id FROM run_skill_snapshots WHERE project_id=:p AND run_id=:run"),
                {"p": UUID(project), "run": run_id},
            )
        assert persisted == UUID(skill_id)
        assert undeclared_tool not in {
            item["id"] for item in gateway.available_role_tools(UUID(project), run_id, profile_id)
        }
        with pytest.raises(ToolDenied):
            gateway.invoke_role_tool(
                UUID(project),
                run_id,
                profile_id,
                f"revoked-{format}",
                undeclared_tool,
                {},
                tool_version=1,
            )
    finally:
        engine.dispose()
