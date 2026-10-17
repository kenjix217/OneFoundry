"""Inert extension lifecycle and exact inbox decisions on migrated PostgreSQL."""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.gateway_extension_identity import canonical_tool_id
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.skill_store import checked_procedures
from onefoundry.tool_gateway import ToolGateway
from tests.test_role_tool_admission_postgres import _run

ORIGIN = {"origin": "http://localhost:8765"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    secret_root = tmp_path / "model-secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
        extension_allowed_origins=["https://api.example.test"],
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as test_client:
        yield test_client


def _project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name}, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _package(client: TestClient, project_id: str) -> dict[str, object]:
    response = client.post(
        f"/api/projects/{project_id}/extensions/packages",
        json={
            "manifest": {
                "schema_version": 1,
                "id": "review-pack",
                "version": "1.0.0",
                "name": "Review pack",
                "description": "Inert review procedures",
                "tools": [],
                "connections": [],
                "skills": [
                    {
                        "id": "review",
                        "name": "Review notes",
                        "description": "Review notes",
                        "content": "# Review notes\nCheck the saved result.",
                        "required_tools": [],
                    }
                ],
            },
            "files": {},
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _change_package(client: TestClient, project_id: str, package: dict[str, object], action: str) -> None:
    package_id = package["id"]
    current = client.get(f"/api/projects/{project_id}/extensions/packages/{package_id}").json()
    request = {
        "expected_sha256": current["sha256"],
        "expected_version": current["package_version"],
        "expected_revision": current["revision"],
    }
    pending = client.post(
        f"/api/projects/{project_id}/extensions/packages/{package_id}/{action}",
        json=request,
        headers=ORIGIN,
    )
    assert pending.status_code == 200, pending.text
    accepted = client.post(
        f"/api/projects/{project_id}/approvals/{pending.json()['approval_id']}/decision",
        json={"decision": "approved"},
        headers=ORIGIN,
    )
    assert accepted.status_code == 200, accepted.text


def test_package_review_is_exact_idempotent_and_project_scoped(client: TestClient) -> None:
    project_id = _project(client, "Extension review")
    other_id = _project(client, "Other extension review")
    package = _package(client, project_id)
    package_id = package["id"]
    assert client.get(f"/api/projects/{other_id}/extensions/packages/{package_id}").status_code == 404
    base = f"/api/projects/{project_id}/extensions/packages/{package_id}"
    request = {
        "expected_sha256": package["sha256"],
        "expected_version": package["package_version"],
        "expected_revision": package["revision"],
    }
    first = client.post(f"{base}/activate", json=request, headers=ORIGIN)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending"
    again = client.post(f"{base}/activate", json=request, headers=ORIGIN)
    assert again.status_code == 200, again.text
    assert again.json()["approval_id"] == first.json()["approval_id"]
    stale = client.post(f"{base}/activate", json={**request, "expected_sha256": "0" * 64}, headers=ORIGIN)
    assert stale.status_code == 409
    decision = client.post(
        f"/api/projects/{project_id}/approvals/{first.json()['approval_id']}/decision",
        json={"decision": "approved"},
        headers=ORIGIN,
    )
    assert decision.status_code == 200, decision.text
    activated = client.get(base).json()
    assert activated["state"] == "active"
    assert activated["revision"] == 2
    retry = client.post(f"{base}/activate", json=request, headers=ORIGIN)
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "approved"
    assert client.get(f"/api/projects/{project_id}/skills").json()[0]["name"] == "review-pack: Review notes"


def test_operator_skill_draft_accepts_exact_hash_once(client: TestClient) -> None:
    project_id = _project(client, "Skill foundry")
    base = f"/api/projects/{project_id}/extensions/drafts"
    response = client.post(
        base,
        json={
            "kind": "skill",
            "name": "Review writing",
            "description": "A procedure",
            "content": "# Review writing\nCheck citations.",
            "required_tools": [],
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["status"] == "pending"
    assert client.get(f"/api/projects/{project_id}/skills").json() == []
    decision = client.post(
        f"{base}/{draft['id']}/decision",
        json={"expected_sha256": draft["sha256"], "decision": "accept"},
        headers=ORIGIN,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["accepted_skill_version"] == 1
    repeated = client.post(
        f"{base}/{draft['id']}/decision",
        json={"expected_sha256": draft["sha256"], "decision": "accept"},
        headers=ORIGIN,
    )
    assert repeated.status_code == 200
    assert repeated.json()["accepted_skill_id"] == decision.json()["accepted_skill_id"]
    assert len(client.get(f"/api/projects/{project_id}/skills").json()) == 1
    reject = client.post(
        f"{base}/{draft['id']}/decision",
        json={"expected_sha256": draft["sha256"], "decision": "reject"},
        headers=ORIGIN,
    )
    assert reject.status_code == 409


def test_existing_skill_draft_requires_exact_base_version(client: TestClient) -> None:
    project_id = _project(client, "Skill revisions")
    skills = f"/api/projects/{project_id}/skills"
    created = client.post(
        skills, json={"name": "Account brief", "description": "A reusable procedure"}, headers=ORIGIN
    )
    assert created.status_code == 201, created.text
    skill_id = created.json()["id"]
    versions = f"{skills}/{skill_id}/versions"
    first = client.post(versions, json={"content": "# Account brief\nFirst."}, headers=ORIGIN)
    assert first.status_code == 201, first.text
    drafts = f"/api/projects/{project_id}/extensions/drafts"
    proposal = {
        "kind": "skill",
        "name": "Account brief",
        "description": "A reusable procedure",
        "content": "# Account brief\nProposed second.",
        "target_skill_id": skill_id,
        "expected_base_version": 1,
    }
    missing_base = client.post(
        drafts,
        json={key: value for key, value in proposal.items() if key != "expected_base_version"},
        headers=ORIGIN,
    )
    assert missing_base.status_code == 422
    pending = client.post(drafts, json=proposal, headers=ORIGIN)
    assert pending.status_code == 201, pending.text
    assert len(client.get(versions).json()) == 1
    second = client.post(versions, json={"content": "# Account brief\nIndependent second."}, headers=ORIGIN)
    assert second.status_code == 201, second.text
    stale = client.post(
        f"{drafts}/{pending.json()['id']}/decision",
        json={"expected_sha256": pending.json()["sha256"], "decision": "accept"},
        headers=ORIGIN,
    )
    assert stale.status_code == 409
    assert len(client.get(versions).json()) == 2
    current = client.post(drafts, json={**proposal, "expected_base_version": 2}, headers=ORIGIN)
    assert current.status_code == 201, current.text
    accepted = client.post(
        f"{drafts}/{current.json()['id']}/decision",
        json={"expected_sha256": current.json()["sha256"], "decision": "accept"},
        headers=ORIGIN,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["accepted_skill_id"] == skill_id
    assert accepted.json()["accepted_skill_version"] == 3


def test_agent_skill_proposal_saves_pending_draft_with_run_attribution(client: TestClient) -> None:
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        project_id, run_id, profile_id = _run(client, gateway, tools=["propose_extension_skill_v1"])
        advertised = gateway.available_role_tools(project_id, run_id, profile_id)
        assert "propose_extension_skill_v1" in {item["id"] for item in advertised}
        result = gateway.invoke_role_tool(
            project_id,
            run_id,
            profile_id,
            "skill-proposal-1",
            "propose_extension_skill_v1",
            {"name": "Source notes", "content": "# Source notes\nCheck the source."},
            tool_version=1,
        )
        assert result["status"] == "done"
        draft = client.get(
            f"/api/projects/{project_id}/extensions/drafts/{result['result']['draft_id']}"
        ).json()
        assert draft["status"] == "pending"
        assert draft["source_run_id"] == str(run_id)
        assert draft["actor_id"] == f"profile:{profile_id}"
        assert client.get(f"/api/projects/{project_id}/skills").json() == []
    finally:
        engine.dispose()


def _connected_package(
    client: TestClient,
    project_id: str,
    key: str,
    kind: str = "http",
    method: str = "GET",
    operation_path: str = "/read",
) -> dict[str, object]:
    response = client.post(
        f"/api/projects/{project_id}/extensions/packages",
        json={
            "manifest": {
                "schema_version": 1,
                "id": key,
                "version": "1.0.0",
                "name": key,
                "connections": [
                    {
                        "id": "source",
                        "name": "Source",
                        "kind": kind,
                        "operations": [{"id": "read", "method": method, "path": operation_path}],
                    }
                ],
            },
            "files": {},
        },
        headers=ORIGIN,
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def test_saved_connection_binding_is_project_scoped_compatible_and_versioned(client: TestClient) -> None:
    project_id = _project(client, "Shared connection")
    other_id = _project(client, "Separate connection")
    source = _connected_package(client, project_id, "connection-source")
    target = _connected_package(client, project_id, "connection-target")
    incompatible = _connected_package(client, project_id, "connection-incompatible", "webhook")
    other = _connected_package(client, other_id, "other-connection")

    def create_connection(package_id: object, owner: str = project_id) -> dict[str, object]:
        response = client.post(
            f"/api/projects/{owner}/extensions/connections",
            json={
                "package_id": package_id,
                "template_id": "source",
                "label": "Example source",
                "config": {"transport": "streamable_http", "endpoint": "https://api.example.test/v1"},
            },
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text
        return response.json()  # type: ignore[no-any-return]

    connection = create_connection(source["id"])
    other_connection = create_connection(other["id"], other_id)
    base = f"/api/projects/{project_id}/extensions/packages/{target['id']}/bindings"
    assert client.get(base).json() == []
    cross_project = client.put(
        f"{base}/source",
        json={"connection_id": other_connection["id"], "expected_version": 0},
        headers=ORIGIN,
    )
    assert cross_project.status_code == 404
    unknown_slot = client.put(
        f"{base}/missing",
        json={"connection_id": connection["id"], "expected_version": 0},
        headers=ORIGIN,
    )
    assert unknown_slot.status_code == 409
    incompatible_base = f"/api/projects/{project_id}/extensions/packages/{incompatible['id']}/bindings"
    mismatch = client.put(
        f"{incompatible_base}/source",
        json={"connection_id": connection["id"], "expected_version": 0},
        headers=ORIGIN,
    )
    assert mismatch.status_code == 409
    created = client.put(
        f"{base}/source",
        json={"connection_id": connection["id"], "expected_version": 0},
        headers=ORIGIN,
    )
    assert created.status_code == 200, created.text
    assert created.json() == {
        "package_id": target["id"],
        "slot": "source",
        "connection_id": connection["id"],
        "version": 1,
    }
    assert client.get(base).json() == [created.json()]
    stale = client.put(
        f"{base}/source",
        json={"connection_id": connection["id"], "expected_version": 0},
        headers=ORIGIN,
    )
    assert stale.status_code == 409
    repeated = client.put(
        f"{base}/source",
        json={"connection_id": connection["id"], "expected_version": 1},
        headers=ORIGIN,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["version"] == 2


def test_saved_connection_binding_rejects_changed_http_operation_contract(
    client: TestClient,
) -> None:
    project_id = _project(client, "Operation contract")
    source = _connected_package(client, project_id, "operation-source")
    changed_method = _connected_package(
        client, project_id, "operation-method", method="POST", operation_path="/read"
    )
    changed_path = _connected_package(
        client, project_id, "operation-path", method="GET", operation_path="/other"
    )
    connection = client.post(
        f"/api/projects/{project_id}/extensions/connections",
        json={
            "package_id": source["id"],
            "template_id": "source",
            "label": "Bound API",
            "config": {"transport": "streamable_http", "endpoint": "https://api.example.test/v1"},
        },
        headers=ORIGIN,
    )
    assert connection.status_code == 201, connection.text
    for package in (changed_method, changed_path):
        binding = client.put(
            f"/api/projects/{project_id}/extensions/packages/{package['id']}/bindings/source",
            json={"connection_id": connection.json()["id"], "expected_version": 0},
            headers=ORIGIN,
        )
        assert binding.status_code == 409, binding.text


def test_canonical_builtin_and_project_extension_prerequisites_without_package_id(
    client: TestClient,
) -> None:
    project_id = _project(client, "Canonical prerequisites")
    other_id = _project(client, "Foreign prerequisites")

    def package(owner: str, key: str) -> dict[str, object]:
        response = client.post(
            f"/api/projects/{owner}/extensions/packages",
            json={
                "manifest": {
                    "schema_version": 1,
                    "id": key,
                    "version": "1.0.0",
                    "name": key,
                    "tools": [
                        {
                            "id": "lookup",
                            "description": "Lookup",
                            "effect": "read",
                            "runtime": "http",
                            "connection": "api",
                            "operation": "lookup",
                            "parameters": {"type": "object", "additionalProperties": False},
                            "returns": {"type": "object", "additionalProperties": False},
                        }
                    ],
                    "connections": [
                        {
                            "id": "api",
                            "name": "API",
                            "kind": "http",
                            "operations": [{"id": "lookup", "method": "GET", "path": "/lookup"}],
                        }
                    ],
                },
                "files": {},
            },
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text
        return response.json()  # type: ignore[no-any-return]

    own = package(project_id, "canonical-own")
    foreign = package(other_id, "canonical-foreign")
    _change_package(client, project_id, own, "activate")
    _change_package(client, other_id, foreign, "activate")
    draft_url = f"/api/projects/{project_id}/extensions/drafts"
    base = {"name": "Source review", "content": "# Source review\nRead sources."}
    own_tool = canonical_tool_id("canonical-own", "lookup")
    for rejected in ("not_registered_v1", canonical_tool_id("canonical-foreign", "lookup")):
        response = client.post(draft_url, json={**base, "required_tools": [rejected]}, headers=ORIGIN)
        assert response.status_code == 409, response.text
    saved = client.post(
        draft_url,
        json={**base, "required_tools": ["current_work_order", own_tool]},
        headers=ORIGIN,
    )
    assert saved.status_code == 201, saved.text
    draft = saved.json()
    assert draft["package_id"] is None
    assert draft["required_tools"] == ["current_work_order", own_tool]
    accepted = client.post(
        f"{draft_url}/{draft['id']}/decision",
        json={"expected_sha256": draft["sha256"], "decision": "accept"},
        headers=ORIGIN,
    )
    assert accepted.status_code == 200, accepted.text
    profile = next(
        item
        for item in client.get(f"/api/projects/{project_id}/profiles").json()
        if item["name"] == "Researcher"
    )
    bound = client.put(
        f"/api/projects/{project_id}/profiles/{profile['id']}/skills/{accepted.json()['accepted_skill_id']}",
        json={"version": 1, "expected_binding_id": None},
        headers=ORIGIN,
    )
    assert bound.status_code == 200, bound.text
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            rows = list(
                connection.execute(
                    text(
                        "SELECT package_id,tool_id,package_sha256,descriptor_sha256 "
                        "FROM extension_skill_prerequisites WHERE project_id=:p AND skill_id=:skill"
                    ),
                    {"p": project_id, "skill": accepted.json()["accepted_skill_id"]},
                ).mappings()
            )
        assert len(rows) == 2
        builtin = next(row for row in rows if row["tool_id"] == "current_work_order")
        extension = next(row for row in rows if row["tool_id"] == own_tool)
        assert builtin["package_id"] is None
        assert builtin["package_sha256"] is None
        assert extension["package_id"] == UUID(str(own["id"]))
        assert extension["package_sha256"] == own["sha256"]
        assert len(extension["descriptor_sha256"]) == 64
        with engine.connect() as connection:
            assert checked_procedures(connection, UUID(project_id), UUID(profile["id"])) == []
    finally:
        engine.dispose()


def test_package_skill_revoke_hides_empty_prerequisites_and_upgrade_reuses_skill_id(
    client: TestClient,
) -> None:
    project_id = _project(client, "Package skill upgrades")
    first = _package(client, project_id)
    _change_package(client, project_id, first, "activate")
    skills_url = f"/api/projects/{project_id}/skills"
    original = client.get(skills_url).json()[0]
    profile = next(
        item
        for item in client.get(f"/api/projects/{project_id}/profiles").json()
        if item["name"] == "Researcher"
    )
    profile_id = profile["id"]
    bind_url = f"/api/projects/{project_id}/profiles/{profile_id}/skills/{original['id']}"
    pinned = client.put(bind_url, json={"version": 1, "expected_binding_id": None}, headers=ORIGIN)
    assert pinned.status_code == 200, pinned.text
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            assert len(checked_procedures(connection, UUID(project_id), UUID(profile_id))) == 1
        _change_package(client, project_id, first, "revoke")
        with engine.connect() as connection:
            assert checked_procedures(connection, UUID(project_id), UUID(profile_id)) == []
        second = client.post(
            f"/api/projects/{project_id}/extensions/packages",
            json={
                "manifest": {
                    "schema_version": 1,
                    "id": "review-pack",
                    "version": "2.0.0",
                    "name": "Review pack",
                    "description": "Updated review procedures",
                    "tools": [],
                    "connections": [],
                    "skills": [
                        {
                            "id": "review",
                            "name": "Updated review notes",
                            "description": "Updated review notes",
                            "content": "# Review notes\nCheck the updated result.",
                            "required_tools": [],
                        }
                    ],
                },
                "files": {},
            },
            headers=ORIGIN,
        )
        assert second.status_code == 201, second.text
        _change_package(client, project_id, second.json(), "activate")
        upgraded = client.get(skills_url).json()
        assert len(upgraded) == 1
        assert upgraded[0]["id"] == original["id"]
        assert upgraded[0]["latest_version"] == 2
        assert upgraded[0]["name"] == "review-pack: Updated review notes"
        with engine.connect() as connection:
            assert checked_procedures(connection, UUID(project_id), UUID(profile_id)) == []
        repinned = client.put(
            bind_url,
            json={"version": 2, "expected_binding_id": pinned.json()["id"]},
            headers=ORIGIN,
        )
        assert repinned.status_code == 200, repinned.text
        with engine.connect() as connection:
            procedures = checked_procedures(connection, UUID(project_id), UUID(profile_id))
            assert len(procedures) == 1
            assert procedures[0]["version"] == 2
    finally:
        engine.dispose()
