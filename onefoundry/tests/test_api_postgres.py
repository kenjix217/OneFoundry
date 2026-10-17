"""Project/profile API and restricted-role behavior on migrated PostgreSQL."""

import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.main import create_app
from onefoundry.settings import Settings


@pytest.fixture
def runtime_settings() -> Settings:
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
    )


@pytest.fixture
def client(runtime_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(runtime_settings), base_url="http://localhost:8765") as test_client:
        yield test_client


def post(client: TestClient, path: str, body: Mapping[str, object]) -> Response:
    return cast(Response, client.post(path, json=body, headers={"origin": "http://localhost:8765"}))


def patch(client: TestClient, path: str, body: Mapping[str, object]) -> Response:
    return cast(Response, client.patch(path, json=body, headers={"origin": "http://localhost:8765"}))


def test_project_creation_seeds_unconfigured_roles_and_actor(
    client: TestClient, runtime_settings: Settings
) -> None:
    response = post(client, "/api/projects", {"name": "  Alpha  "})
    assert response.status_code == 201
    project = response.json()
    assert project["name"] == "Alpha"
    assert project["goal"] == project["desired_outcome"] == ""
    project_id = UUID(project["id"])
    assert client.get(f"/api/projects/{project_id}").json()["id"] == project["id"]
    assert project in client.get("/api/projects").json()
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    assert {profile["name"] for profile in profiles} == {
        "Chief of Staff",
        "Engineer",
        "QA",
        "Researcher",
        "GTM",
        "Sales",
    }
    chief = next(profile for profile in profiles if profile["name"] == "Chief of Staff")
    assert chief["reports_to_profile_id"] is None
    assert all(profile["reports_to_profile_id"] == chief["id"] for profile in profiles if profile != chief)
    assert all(profile["kind"] == "builtin" for profile in profiles)
    assert all(profile["model"] is None and not profile["configured"] for profile in profiles)
    assert all(profile["instructions"].strip() for profile in profiles)
    assert all(
        profile["memory_scope"] == "company" and profile["autonomy_level"] == "manual" for profile in profiles
    )
    assert all(profile["allowed_tools"] == [] and not profile["permissions_active"] for profile in profiles)
    engine = create_engine(runtime_settings.database_url)
    with engine.connect() as connection:
        actor = connection.scalar(text("SELECT actor_id FROM projects WHERE id = :id"), {"id": project_id})
        profile_actors = cast(
            list[str],
            (
                connection.execute(
                    text("SELECT DISTINCT actor_id FROM profiles WHERE project_id = :id"), {"id": project_id}
                )
                .scalars()
                .all()
            ),
        )
    assert actor == "operator"
    assert profile_actors == ["operator"]
    engine.dispose()


def test_project_brief_validation_conflict_and_isolation(client: TestClient) -> None:
    first = post(client, "/api/projects", {"name": "Brief first"}).json()
    second = post(client, "/api/projects", {"name": "Brief second"}).json()
    path = f"/api/projects/{first['id']}/brief"
    body = {"goal": "  Improve handoffs  ", "desired_outcome": "  QA has evidence  ", "expected_version": 1}
    for invalid in (
        {**body, "goal": "   "},
        {**body, "desired_outcome": "   "},
        {**body, "goal": "Use sk-testonlysecretvalue123456"},
        {**body, "desired_outcome": "token: abc123"},
        {**body, "expected_version": 0},
        {**body, "extra": "field"},
    ):
        response = patch(client, path, invalid)
        assert response.status_code == 422
        assert "sk-testonlysecretvalue123456" not in response.text
    updated = patch(client, path, body)
    assert updated.status_code == 200
    assert updated.json()["goal"] == "Improve handoffs"
    assert updated.json()["desired_outcome"] == "QA has evidence"
    assert updated.json()["version"] == 2
    assert client.get(f"/api/projects/{first['id']}").json() == updated.json()
    assert patch(client, path, body).status_code == 409
    assert client.get(f"/api/projects/{second['id']}").json()["goal"] == ""
    assert patch(client, f"/api/projects/{uuid4()}/brief", body).status_code == 404
    archived = post(client, f"/api/projects/{first['id']}/archive", {"expected_version": 2})
    assert archived.status_code == 200
    assert archived.json()["goal"] == "Improve handoffs"
    assert patch(client, path, {**body, "expected_version": 3}).status_code == 409


def test_project_plan_is_separate_versioned_record(client: TestClient) -> None:
    first = post(client, "/api/projects", {"name": "Plan first"}).json()
    second = post(client, "/api/projects", {"name": "Plan second"}).json()
    path = f"/api/projects/{first['id']}/plan"
    assert client.get(path).json()["body"] == ""
    assert client.get(path).json()["version"] == 1
    assert client.get(path).json()["actor_id"] == "operator"
    body = {"body": "Engineer completes the build; QA checks each result.", "expected_version": 1}
    for invalid in (
        {**body, "body": "   "},
        {**body, "body": "token: abc123"},
        {**body, "body": "X" * 4001},
        {**body, "expected_version": 0},
        {**body, "extra": "field"},
    ):
        assert patch(client, path, invalid).status_code == 422
    saved = patch(client, path, body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["body"] == body["body"]
    assert saved.json()["version"] == 2
    assert patch(client, path, body).status_code == 409
    assert client.get(f"/api/projects/{second['id']}/plan").json()["body"] == ""
    assert client.get(f"/api/projects/{uuid4()}/plan").status_code == 404
    assert patch(client, f"/api/projects/{uuid4()}/plan", body).status_code == 404
    archived = post(client, f"/api/projects/{first['id']}/archive", {"expected_version": 1})
    assert archived.status_code == 200
    assert patch(client, path, {**body, "expected_version": 2}).status_code == 409


def test_starter_backfill_changes_only_blank_builtins_and_is_idempotent(client: TestClient) -> None:
    first = post(client, "/api/projects", {"name": "Starter first"}).json()
    second = post(client, "/api/projects", {"name": "Starter second"}).json()
    first_path = f"/api/projects/{first['id']}/profiles"
    second_before = client.get(f"/api/projects/{second['id']}/profiles").json()
    profiles = client.get(first_path).json()
    engineer = next(profile for profile in profiles if profile["name"] == "Engineer")
    qa = next(profile for profile in profiles if profile["name"] == "QA")
    assert patch(client, f"{first_path}/{engineer['id']}", {"instructions": " "}).status_code == 200
    assert (
        patch(
            client,
            f"{first_path}/{qa['id']}",
            {
                "instructions": "Custom QA instructions",
                "model": "test-model",
                "allowed_tools": ["web_search"],
            },
        ).status_code
        == 200
    )
    custom = post(client, first_path, {"name": "Specialist", "instructions": "Custom role"}).json()
    backfilled = post(client, f"{first_path}/starter", {})
    assert backfilled.status_code == 200
    rows = backfilled.json()
    assert next(row for row in rows if row["id"] == engineer["id"])["instructions"].strip()
    qa_after = next(row for row in rows if row["id"] == qa["id"])
    assert qa_after["instructions"] == "Custom QA instructions"
    assert qa_after["model"] == "test-model" and qa_after["allowed_tools"] == ["web_search"]
    assert next(row for row in rows if row["id"] == custom["id"])["instructions"] == "Custom role"
    assert post(client, f"{first_path}/starter", {}).json() == rows
    assert client.get(f"/api/projects/{second['id']}/profiles").json() == second_before
    assert post(client, f"/api/projects/{uuid4()}/profiles/starter", {}).status_code == 404
    archived = post(client, f"/api/projects/{first['id']}/archive", {"expected_version": 1})
    assert archived.status_code == 200
    assert post(client, f"{first_path}/starter", {}).status_code == 409


def test_custom_profile_and_builtin_edit(client: TestClient) -> None:
    project = post(client, "/api/projects", {"name": "Profiles"}).json()
    path = f"/api/projects/{project['id']}/profiles"
    custom = post(
        client,
        path,
        {
            "name": "Writer",
            "model": "provider/model-1",
            "instructions": "Draft text",
            "memory_scope": "project",
            "allowed_tools": ["web_search"],
            "autonomy_level": "supervised",
        },
    )
    assert custom.status_code == 201
    body = custom.json()
    assert body["kind"] == "custom" and body["configured"] is True
    assert body["allowed_tools"] == ["web_search"]
    assert body["permissions_active"] is False
    engineer = client.get(path).json()[0]
    edited = patch(
        client,
        f"{path}/{engineer['id']}",
        {
            "model": "provider/model-1",
            "instructions": "Implement tasks",
            "allowed_tools": ["read_repository"],
            "autonomy_level": "bounded",
        },
    )
    assert edited.status_code == 200
    edited_body = edited.json()
    assert edited_body["kind"] == "builtin" and edited_body["configured"] is True
    assert edited_body["permissions_active"] is False
    cleared = patch(client, f"{path}/{engineer['id']}", {"model": None})
    assert cleared.json()["configured"] is False


def test_profile_reporting_tree_validates_project_and_cycles(client: TestClient) -> None:
    first = post(client, "/api/projects", {"name": "Reporting first"}).json()
    second = post(client, "/api/projects", {"name": "Reporting second"}).json()
    path = f"/api/projects/{first['id']}/profiles"
    profiles = client.get(path).json()
    chief = next(item for item in profiles if item["name"] == "Chief of Staff")
    foreign_chief = next(
        item
        for item in client.get(f"/api/projects/{second['id']}/profiles").json()
        if item["name"] == "Chief of Staff"
    )
    first_custom = post(client, path, {"name": "First custom", "instructions": "Plan"}).json()
    assert first_custom["reports_to_profile_id"] == chief["id"]
    confusable = post(client, path, {"name": "chief of ſtaff", "instructions": "Plan"})
    assert confusable.status_code == 201
    assert confusable.json()["reports_to_profile_id"] == chief["id"]
    assert (
        patch(client, f"{path}/{confusable.json()['id']}", {"reports_to_profile_id": None}).status_code == 409
    )
    assert (
        post(
            client, path, {"name": "Extra root", "instructions": "Plan", "reports_to_profile_id": None}
        ).status_code
        == 409
    )
    assert (
        post(
            client,
            path,
            {"name": "Wrong project", "instructions": "Plan", "reports_to_profile_id": foreign_chief["id"]},
        ).status_code
        == 404
    )
    assert (
        patch(
            client, f"{path}/{first_custom['id']}", {"reports_to_profile_id": first_custom["id"]}
        ).status_code
        == 409
    )
    assert (
        patch(client, f"{path}/{chief['id']}", {"reports_to_profile_id": first_custom["id"]}).status_code
        == 409
    )
    assert patch(client, f"{path}/{chief['id']}", {"reports_to_profile_id": None}).status_code == 200
    assert patch(client, f"{path}/{first_custom['id']}", {"reports_to_profile_id": None}).status_code == 409
    second_custom = post(
        client,
        path,
        {"name": "Second custom", "instructions": "Verify", "reports_to_profile_id": first_custom["id"]},
    ).json()
    assert second_custom["reports_to_profile_id"] == first_custom["id"]
    assert (
        patch(
            client, f"{path}/{first_custom['id']}", {"reports_to_profile_id": second_custom["id"]}
        ).status_code
        == 409
    )
    assert (
        patch(
            client, f"{path}/{second_custom['id']}", {"reports_to_profile_id": foreign_chief["id"]}
        ).status_code
        == 404
    )
    saved = {item["id"]: item for item in client.get(path).json()}
    assert saved[first_custom["id"]]["reports_to_profile_id"] == chief["id"]
    assert saved[second_custom["id"]]["reports_to_profile_id"] == first_custom["id"]


def test_concurrent_reporting_changes_cannot_form_cycle(client: TestClient) -> None:
    project = post(client, "/api/projects", {"name": "Concurrent reporting"}).json()
    path = f"/api/projects/{project['id']}/profiles"
    a = post(client, path, {"name": "A", "instructions": "Plan"}).json()
    b = post(client, path, {"name": "B", "instructions": "Plan"}).json()
    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(patch, client, f"{path}/{a['id']}", {"reports_to_profile_id": b["id"]})
        two = executor.submit(patch, client, f"{path}/{b['id']}", {"reports_to_profile_id": a["id"]})
        statuses = {one.result().status_code, two.result().status_code}
    assert statuses == {200, 409}
    saved = {item["id"]: item for item in client.get(path).json()}
    assert not (
        saved[a["id"]]["reports_to_profile_id"] == b["id"]
        and saved[b["id"]]["reports_to_profile_id"] == a["id"]
    )


def test_profile_instructions_reject_and_redact_secret_like_text(client: TestClient) -> None:
    project = post(client, "/api/projects", {"name": "Profile instruction boundary"}).json()
    path = f"/api/projects/{project['id']}/profiles"
    marker = "sk-testonlysecretvalue123456"
    rejected = post(
        client,
        path,
        {"name": "Unsafe", "model": "test-model", "instructions": f"Use {marker}"},
    )
    assert rejected.status_code == 422
    assert marker not in rejected.text
    assert not any(profile["name"] == "Unsafe" for profile in client.get(path).json())
    profile = post(
        client, path, {"name": "Safe", "model": "test-model", "instructions": "Write a draft"}
    ).json()
    rejected_patch = patch(client, f"{path}/{profile['id']}", {"instructions": f"Use {marker}"})
    assert rejected_patch.status_code == 422
    assert marker not in rejected_patch.text
    saved = next(item for item in client.get(path).json() if item["id"] == profile["id"])
    assert saved["instructions"] == "Write a draft"


def test_long_profile_instructions_round_trip_without_truncation(client: TestClient) -> None:
    project = post(client, "/api/projects", {"name": "Long profile instructions"}).json()
    path = f"/api/projects/{project['id']}/profiles"
    original = "Review carefully.\n" * 1_000
    updated = "Follow the complete procedure.\n" * 1_500
    assert len(original) > 10_000
    assert len(updated) > 10_000

    created = post(client, path, {"name": "Long profile", "instructions": original})
    assert created.status_code == 201
    profile_id = created.json()["id"]
    assert created.json()["instructions"] == original.strip()

    saved = patch(client, f"{path}/{profile_id}", {"instructions": updated})
    assert saved.status_code == 200
    assert saved.json()["instructions"] == updated.strip()
    assert (
        next(profile for profile in client.get(path).json() if profile["id"] == profile_id)["instructions"]
        == updated.strip()
    )

    at_limit = "x" * 50_000
    assert (
        patch(client, f"{path}/{profile_id}", {"instructions": at_limit}).json()["instructions"] == at_limit
    )

    too_long = "x" * 50_001
    rejected = patch(client, f"{path}/{profile_id}", {"instructions": too_long})
    assert rejected.status_code == 422
    assert (
        next(profile for profile in client.get(path).json() if profile["id"] == profile_id)["instructions"]
        == at_limit
    )


def test_invalid_input_conflict_and_project_isolation(client: TestClient) -> None:
    first = post(client, "/api/projects", {"name": "First"}).json()
    second = post(client, "/api/projects", {"name": "Second"}).json()
    first_path = f"/api/projects/{first['id']}/profiles"
    second_path = f"/api/projects/{second['id']}/profiles"
    assert post(client, "/api/projects", {"name": "  "}).status_code == 422
    assert post(client, "/api/projects", {"name": "Bad", "actor_id": "agent"}).status_code == 422
    assert (
        post(client, first_path, {"name": "Blank", "model": " ", "instructions": "text"}).status_code == 422
    )
    assert (
        post(client, first_path, {"name": "Blank", "model": "model", "instructions": " "}).status_code == 422
    )
    assert (
        post(
            client,
            first_path,
            {"name": "Bad", "model": "m", "instructions": "x", "allowed_tools": ["bad tool"]},
        ).status_code
        == 422
    )
    assert (
        post(
            client,
            first_path,
            {"name": "Bad", "model": "m", "instructions": "x", "autonomy_level": "unrestricted"},
        ).status_code
        == 422
    )
    good: dict[str, object] = {"name": "Writer", "model": "provider/model", "instructions": "Draft"}
    assert post(client, first_path, good).status_code == 201
    conflict = post(client, first_path, {**good, "name": "writer"})
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "profile name already exists"}
    assert post(client, second_path, good).status_code == 201
    first_profile_id = client.get(first_path).json()[-1]["id"]
    for field in ("instructions", "memory_scope", "allowed_tools", "autonomy_level"):
        assert patch(client, f"{first_path}/{first_profile_id}", {field: None}).status_code == 422
    assert patch(client, f"{first_path}/{first_profile_id}", {"kind": "builtin"}).status_code == 422
    assert patch(client, f"{first_path}/{first_profile_id}", {"name": "Renamed"}).status_code == 422
    foreign_id = client.get(second_path).json()[-1]["id"]
    assert patch(client, f"{first_path}/{foreign_id}", {"instructions": "No"}).status_code == 404
    assert client.get(f"/api/projects/{uuid4()}/profiles").status_code == 404
    assert post(client, f"/api/projects/{uuid4()}/profiles", good).status_code == 404


def test_runtime_role_has_only_required_privileges(runtime_settings: Settings) -> None:
    engine = create_engine(runtime_settings.database_url)
    with engine.connect() as connection:
        role = connection.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
        rights = connection.execute(
            text(
                "SELECT has_table_privilege(current_user, 'projects', 'INSERT'), "
                "has_table_privilege(current_user, 'projects', 'UPDATE'), "
                "has_column_privilege(current_user, 'projects', 'goal', 'UPDATE'), "
                "has_column_privilege(current_user, 'projects', 'desired_outcome', 'UPDATE'), "
                "has_column_privilege(current_user, 'projects', 'name', 'UPDATE'), "
                "has_column_privilege(current_user, 'profiles', 'model', 'UPDATE'), "
                "has_column_privilege(current_user, 'profiles', 'name', 'UPDATE'), "
                "has_table_privilege(current_user, 'events', 'INSERT'), "
                "has_table_privilege(current_user, 'events', 'UPDATE'), "
                "has_table_privilege(current_user, 'events', 'DELETE'), "
                "has_table_privilege(current_user, 'events', 'TRUNCATE')"
            )
        ).one()
    assert tuple(role) == (False, False, False, False, False)
    assert tuple(rights) == (True, False, True, True, False, True, False, True, False, False, False)
    for statement in ("UPDATE events SET event_type = 'x'", "DELETE FROM events", "TRUNCATE events"):
        with pytest.raises(DBAPIError, match="permission denied"):
            with engine.begin() as connection:
                connection.execute(text(statement))
    engine.dispose()
