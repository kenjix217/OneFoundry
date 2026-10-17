"""Project archive and recoverable Trash against migrated PostgreSQL."""

import os
from collections.abc import Iterator
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as test_client:
        yield test_client


def post(client: TestClient, path: str, body: dict[str, object] | None = None) -> Response:
    return cast(Response, client.post(path, json=body, headers={"origin": "http://localhost:8765"}))


def test_archive_trash_restore_preserves_project_data_and_events(
    client: TestClient, settings: Settings
) -> None:
    project = post(client, "/api/projects", {"name": "Recoverable"}).json()
    root = f"/api/projects/{project['id']}"
    profiles = client.get(f"{root}/profiles").json()
    assert len(profiles) == 6
    archived = post(client, f"{root}/archive", {"expected_version": 1})
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived" and archived.json()["version"] == 2
    assert project["id"] not in {item["id"] for item in client.get("/api/projects").json()}
    assert project["id"] in {item["id"] for item in client.get("/api/projects?status=archived").json()}
    assert post(client, f"{root}/profiles", {"name": "Blocked", "instructions": "No"}).status_code == 409
    assert post(client, f"{root}/archive", {"expected_version": 1}).status_code == 409
    assert post(client, f"{root}/trash", {"expected_version": 2, "confirm_name": "Wrong"}).status_code == 409
    trashed = post(client, f"{root}/trash", {"expected_version": 2, "confirm_name": "Recoverable"})
    assert trashed.status_code == 200, trashed.text
    assert trashed.json()["status"] == "trashed" and trashed.json()["trash_from_status"] == "archived"
    assert project["id"] in {item["id"] for item in client.get("/api/projects?status=trashed").json()}
    assert len(client.get(f"{root}/profiles").json()) == 6
    restored = post(client, f"{root}/restore", {"expected_version": 3})
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "archived" and restored.json()["version"] == 4
    active = post(client, f"{root}/unarchive", {"expected_version": 4})
    assert active.status_code == 200, active.text
    assert active.json()["status"] == "active" and active.json()["version"] == 5
    assert post(client, f"{root}/profiles", {"name": "Planner", "instructions": "Plan"}).status_code == 201
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        event_types = cast(
            list[str],
            connection.execute(
                text("SELECT event_type FROM events WHERE project_id=:id ORDER BY created_at"),
                {"id": UUID(project["id"])},
            )
            .scalars()
            .all(),
        )
    engine.dispose()
    assert event_types[-4:] == ["project_archive", "project_trash", "project_restore", "project_unarchive"]


def test_running_work_must_finish_before_archive_or_trash(client: TestClient) -> None:
    project = post(client, "/api/projects", {"name": "Busy"}).json()
    root = f"/api/projects/{project['id']}"
    profile_id = client.get(f"{root}/profiles").json()[0]["id"]
    order = post(
        client, f"{root}/work-orders", {"title": "Draft", "instructions": "Do work", "profile_id": profile_id}
    ).json()
    assert post(client, f"{root}/work-orders/{order['id']}/ready").status_code == 200
    assert post(client, f"{root}/work-orders/{order['id']}/runs").status_code == 201
    assert post(client, f"{root}/archive", {"expected_version": 1}).status_code == 409
    assert post(client, f"{root}/trash", {"expected_version": 1, "confirm_name": "Busy"}).status_code == 409
    assert client.get(root).json()["status"] == "active"


def test_project_lifecycle_invalid_status_and_origin(client: TestClient) -> None:
    project = post(client, "/api/projects", {"name": "Boundary"}).json()
    root = f"/api/projects/{project['id']}"
    assert client.get("/api/projects?status=missing").status_code == 422
    assert (
        post(client, f"{root}/trash", {"expected_version": 0, "confirm_name": "Boundary"}).status_code == 422
    )
    assert (
        client.post(
            f"{root}/archive", json={"expected_version": 1}, headers={"origin": "http://evil.test"}
        ).status_code
        == 403
    )
