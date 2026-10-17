"""Validate project-local profile reporting lines under the project row lock."""

from typing import cast
from uuid import UUID

from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict, NotFound


def lock_project(connection: Connection, project_id: UUID) -> None:
    status = connection.scalar(
        text("SELECT status FROM projects WHERE id=:id FOR UPDATE"), {"id": project_id}
    )
    if status is None:
        raise NotFound("project not found")
    if status != "active":
        raise Conflict("restore this project before changing its workspace")


def chief_profile_id(connection: Connection, project_id: UUID) -> UUID:
    chief_id = connection.scalar(
        text("SELECT id FROM profiles WHERE project_id=:project_id AND lower(name)='chief of staff'"),
        {"project_id": project_id},
    )
    if chief_id is None:
        raise Conflict("Chief of Staff profile is missing")
    return cast(UUID, chief_id)


def validate_parent(
    connection: Connection, project_id: UUID, profile_id: UUID, parent_id: UUID | None
) -> None:
    if parent_id is None:
        raise Conflict("only Chief of Staff may report directly to the operator")
    if parent_id == profile_id:
        raise Conflict("profile cannot report to itself")
    ancestors = cast(
        list[UUID],
        connection.execute(
            text(
                "WITH RECURSIVE ancestors AS ("
                "SELECT id, reports_to_profile_id, ARRAY[id] AS path FROM profiles "
                "WHERE project_id=:project_id AND id=:parent_id "
                "UNION ALL SELECT p.id, p.reports_to_profile_id, a.path || p.id "
                "FROM profiles p JOIN ancestors a ON p.id=a.reports_to_profile_id "
                "WHERE p.project_id=:project_id AND NOT p.id=ANY(a.path)) "
                "SELECT id FROM ancestors"
            ),
            {"project_id": project_id, "parent_id": parent_id},
        )
        .scalars()
        .all(),
    )
    if not ancestors:
        raise NotFound("reporting parent not found in project")
    if profile_id in ancestors:
        raise Conflict("reporting relationship would create a cycle")
