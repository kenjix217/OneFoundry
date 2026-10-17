"""Project-scoped profile settings and the latest saved run observation."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, text

from onefoundry.gateway_errors import NotFound
from onefoundry.workflow_schemas import RunStatus


class CapabilityRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    status: RunStatus
    tools_active: bool | None


class CapabilityReadback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    profile_id: UUID
    configured_model_id: str | None
    configured_tool_ids: list[str]
    last_run: CapabilityRun | None


class OneFoundryCapabilityReader:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def read_capability(self, project_id: UUID, profile_id: UUID) -> CapabilityReadback:
        # One statement keeps the profile, latest run, and its admission event
        # in one database snapshot. Only a recorded boolean is an observation.
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT p.project_id,p.id AS profile_id,p.model,p.allowed_tools,"
                        "r.id AS run_id,r.status AS run_status,e.tools_active "
                        "FROM profiles p "
                        "LEFT JOIN LATERAL ("
                        "SELECT id,status FROM runs WHERE project_id=p.project_id AND profile_id=p.id "
                        "ORDER BY started_at DESC,id DESC LIMIT 1"
                        ") r ON true "
                        "LEFT JOIN LATERAL ("
                        "SELECT CASE WHEN jsonb_typeof(payload->'tools_active')='boolean' "
                        "THEN (payload->>'tools_active')::boolean ELSE NULL END AS tools_active "
                        "FROM events WHERE project_id=p.project_id AND run_id=r.id "
                        "AND event_type='live_text_started' "
                        "ORDER BY created_at DESC,id DESC LIMIT 1"
                        ") e ON true "
                        "WHERE p.project_id=:project_id AND p.id=:profile_id"
                    ),
                    {"project_id": project_id, "profile_id": profile_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise NotFound("profile not found")
        return CapabilityReadback(
            project_id=row["project_id"],
            profile_id=row["profile_id"],
            configured_model_id=row["model"],
            configured_tool_ids=row["allowed_tools"],
            last_run=(
                CapabilityRun(id=row["run_id"], status=row["run_status"], tools_active=row["tools_active"])
                if row["run_id"] is not None
                else None
            ),
        )
