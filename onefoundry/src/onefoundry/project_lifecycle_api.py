"""HTTP routes for reversible project lifecycle changes."""

from uuid import UUID

from fastapi import APIRouter

from onefoundry.schemas import ProjectOut, ProjectTransition, ProjectTrash
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_project_lifecycle_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post("/archive", response_model=ProjectOut)
    def archive(project_id: UUID, data: ProjectTransition) -> ProjectOut:
        return execute(
            lambda: ProjectOut.model_validate(
                gateway.transition_project(project_id, data.expected_version, "archive")
            )
        )

    @router.post("/unarchive", response_model=ProjectOut)
    def unarchive(project_id: UUID, data: ProjectTransition) -> ProjectOut:
        return execute(
            lambda: ProjectOut.model_validate(
                gateway.transition_project(project_id, data.expected_version, "unarchive")
            )
        )

    @router.post("/trash", response_model=ProjectOut)
    def trash(project_id: UUID, data: ProjectTrash) -> ProjectOut:
        return execute(
            lambda: ProjectOut.model_validate(
                gateway.transition_project(project_id, data.expected_version, "trash", data.confirm_name)
            )
        )

    @router.post("/restore", response_model=ProjectOut)
    def restore(project_id: UUID, data: ProjectTransition) -> ProjectOut:
        return execute(
            lambda: ProjectOut.model_validate(
                gateway.transition_project(project_id, data.expected_version, "restore")
            )
        )

    return router
