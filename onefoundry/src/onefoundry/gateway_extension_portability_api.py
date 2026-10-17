"""Thin operator routes for inert archive import/export."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from pydantic import Field

from onefoundry.gateway_extension_portability import (
    ArchiveRejected,
    PackageFormat,
    export_archive,
    import_archive,
)
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute
from onefoundry.workflow_schemas import StrictModel


class ArchiveImport(StrictModel):
    format: PackageFormat
    archive_base64: str = Field(min_length=4, max_length=5592408, repr=False)


def make_extension_portability_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/extensions")

    @router.post("/packages/import", status_code=201)
    def import_package(project_id: UUID, data: ArchiveImport) -> dict[str, Any]:
        try:
            return execute(lambda: import_archive(gateway, project_id, data.format, data.archive_base64))
        except ArchiveRejected as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/packages/{package_id}/export")
    def export_package(project_id: UUID, package_id: UUID, format: PackageFormat = "onefoundry") -> Response:
        archive, name = execute(lambda: export_archive(gateway, project_id, package_id, format))
        return Response(
            archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store",
            },
        )

    return router
