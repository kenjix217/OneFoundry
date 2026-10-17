"""Read-only operator HTTP view of owner-verified private artifacts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


class ArtifactSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: str
    mime_type: str
    sha256: str
    byte_size: int
    status: str
    created_at: datetime
    finished_at: datetime | None


class ArtifactPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    kind: str
    mime_type: str
    sha256: str
    byte_size: int
    content: str
    playback_verified: bool | None = None


def make_workspace_artifact_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/runs/{run_id}/workspace-artifacts")

    @router.get("", response_model=list[ArtifactSummary])
    def list_saved(project_id: UUID, run_id: UUID) -> list[ArtifactSummary]:
        return execute(
            lambda: [
                ArtifactSummary.model_validate(item)
                for item in gateway.workspace_artifacts(project_id, run_id)
            ]
        )

    @router.get("/{artifact_id}", response_model=ArtifactPreview, response_model_exclude_none=True)
    def read_saved(project_id: UUID, run_id: UUID, artifact_id: UUID) -> ArtifactPreview:
        return execute(
            lambda: ArtifactPreview.model_validate(
                gateway.workspace_artifact(project_id, run_id, artifact_id)
            )
        )

    return router
