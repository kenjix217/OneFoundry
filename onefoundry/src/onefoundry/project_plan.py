"""Versioned project-wide plan persistence, separate from the project goal."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.workflow_schemas import StrictModel, local_text


class ProjectPlanUpdate(StrictModel):
    body: str = Field(min_length=1, max_length=4000)
    expected_version: int = Field(ge=1)

    @field_validator("body")
    @classmethod
    def safe_body(cls, value: str) -> str:
        return local_text(value)


class ProjectPlanOut(StrictModel):
    project_id: UUID
    body: str
    version: int = Field(ge=1)
    actor_id: str
    updated_at: datetime


def read_plan(connection: Connection, project_id: UUID) -> dict[str, Any]:
    row = (
        connection.execute(
            text("SELECT project_id,body,version,actor_id,updated_at FROM project_plans WHERE project_id=:p"),
            {"p": project_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("project plan not found")
    return dict(row)


def update_plan(
    connection: Connection, project_id: UUID, data: ProjectPlanUpdate, actor_id: str
) -> dict[str, Any]:
    lock_project(connection, project_id)
    row = (
        connection.execute(
            text(
                "UPDATE project_plans SET body=:body,actor_id=:actor,version=version+1,"
                "updated_at=clock_timestamp() WHERE project_id=:p AND version=:v "
                "RETURNING project_id,body,version,actor_id,updated_at"
            ),
            {"p": project_id, "body": data.body, "actor": actor_id, "v": data.expected_version},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise Conflict("project plan changed; reload before saving")
    return dict(row)
