"""Project-scoped readback routes for the operator inbox."""

from uuid import UUID

from fastapi import APIRouter

from onefoundry.onefoundry_inbox import inbox
from onefoundry.onefoundry_metrics import outcomes
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_onefoundry_inbox_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/onefoundry")

    @router.get("/inbox")
    def read_inbox(project_id: UUID) -> dict[str, object]:
        def read() -> dict[str, object]:
            with gateway.engine.connect() as connection:
                gateway._require_project(connection, project_id)
                return inbox(connection, project_id)

        return execute(read)

    @router.get("/outcomes")
    def read_outcomes(project_id: UUID) -> dict[str, object]:
        def read() -> dict[str, object]:
            with gateway.engine.connect() as connection:
                gateway._require_project(connection, project_id)
                return outcomes(connection, project_id)

        return execute(read)

    return router
