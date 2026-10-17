"""Real PostgreSQL migration and append-only enforcement."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.settings import Settings


def test_events_reject_update_and_delete() -> None:
    engine = create_engine(Settings().database_url)
    project_id, event_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO projects (id, name, actor_id) VALUES (:id, :name, :actor)"),
            {"id": project_id, "name": "Test", "actor": "operator"},
        )
        connection.execute(
            text(
                "INSERT INTO events (id, project_id, actor_id, event_type) "
                "VALUES (:id, :project, :actor, :kind)"
            ),
            {"id": event_id, "project": project_id, "actor": "operator", "kind": "test"},
        )
    for statement in (
        "UPDATE events SET event_type = 'changed' WHERE id = :id",
        "DELETE FROM events WHERE id = :id",
        "TRUNCATE TABLE events",
    ):
        with pytest.raises(DBAPIError, match="events are append-only"):
            with engine.begin() as connection:
                connection.execute(text(statement), {"id": event_id})
    with engine.connect() as connection:
        count = connection.scalar(text("SELECT count(*) FROM events WHERE id = :id"), {"id": event_id})
        assert count == 1
    engine.dispose()


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("name", "", "projects_name_nonblank"),
        ("name", " \t ", "projects_name_nonblank"),
        ("actor_id", "", "projects_actor_id_nonblank"),
        ("actor_id", " \t ", "projects_actor_id_nonblank"),
    ],
)
def test_projects_reject_blank_fields(field: str, value: str, constraint: str) -> None:
    engine = create_engine(Settings().database_url)
    values = {"id": uuid4(), "name": "Project", "actor_id": "operator"}
    values[field] = value
    with pytest.raises(DBAPIError, match=constraint):
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO projects (id, name, actor_id) VALUES (:id, :name, :actor_id)"),
                values,
            )
    engine.dispose()


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("actor_id", "", "events_actor_id_nonblank"),
        ("actor_id", " \t ", "events_actor_id_nonblank"),
        ("event_type", "", "events_event_type_nonblank"),
        ("event_type", " \t ", "events_event_type_nonblank"),
    ],
)
def test_events_reject_blank_fields(field: str, value: str, constraint: str) -> None:
    engine = create_engine(Settings().database_url)
    project_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO projects (id, name, actor_id) VALUES (:id, :name, :actor_id)"),
            {"id": project_id, "name": "Project", "actor_id": "operator"},
        )
    values = {"id": uuid4(), "project_id": project_id, "actor_id": "operator", "event_type": "test"}
    values[field] = value
    with pytest.raises(DBAPIError, match=constraint):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO events (id, project_id, actor_id, event_type) "
                    "VALUES (:id, :project_id, :actor_id, :event_type)"
                ),
                values,
            )
    engine.dispose()
