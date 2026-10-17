"""Operator HTTP surface for exact extension and skill lifecycle decisions."""

from uuid import UUID

from fastapi import APIRouter

from onefoundry.gateway_extension_schemas import (
    ApprovalPendingOut,
    CatalogOut,
    ConnectionBindingOut,
    ConnectionBindingPut,
    ConnectionCreate,
    ConnectionOut,
    ConnectionPatch,
    DraftCreate,
    DraftDecision,
    DraftOut,
    PackageChange,
    PackageCreate,
    PackageOut,
)
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_extension_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/extensions")

    @router.get("/catalog", response_model=CatalogOut)
    def catalog(project_id: UUID) -> CatalogOut:
        return execute(lambda: CatalogOut.model_validate(gateway._extensions().catalog(project_id)))

    @router.get("/packages", response_model=list[PackageOut])
    def packages(project_id: UUID) -> list[PackageOut]:
        return execute(
            lambda: [PackageOut.model_validate(x) for x in gateway._extensions().list_packages(project_id)]
        )

    @router.get("/packages/{package_id}", response_model=PackageOut)
    def package(project_id: UUID, package_id: UUID) -> PackageOut:
        return execute(
            lambda: PackageOut.model_validate(gateway._extensions().get_package(project_id, package_id))
        )

    @router.post("/packages", response_model=PackageOut, status_code=201)
    def create_package(project_id: UUID, data: PackageCreate) -> PackageOut:
        return execute(
            lambda: PackageOut.model_validate(gateway._extensions().create_package(project_id, data))
        )

    @router.post("/packages/{package_id}/activate", response_model=ApprovalPendingOut)
    def activate(project_id: UUID, package_id: UUID, data: PackageChange) -> ApprovalPendingOut:
        return execute(
            lambda: ApprovalPendingOut.model_validate(
                gateway._extensions().request_package_change(project_id, package_id, "activate", data)
            )
        )

    @router.post("/packages/{package_id}/revoke", response_model=ApprovalPendingOut)
    def revoke(project_id: UUID, package_id: UUID, data: PackageChange) -> ApprovalPendingOut:
        return execute(
            lambda: ApprovalPendingOut.model_validate(
                gateway._extensions().request_package_change(project_id, package_id, "revoke", data)
            )
        )

    @router.get("/connections", response_model=list[ConnectionOut])
    def connections(project_id: UUID) -> list[ConnectionOut]:
        return execute(
            lambda: [
                ConnectionOut.model_validate(x) for x in gateway._extensions().list_connections(project_id)
            ]
        )

    @router.post("/connections", response_model=ConnectionOut, status_code=201)
    def create_connection(project_id: UUID, data: ConnectionCreate) -> ConnectionOut:
        return execute(
            lambda: ConnectionOut.model_validate(gateway._extensions().create_connection(project_id, data))
        )

    @router.patch("/connections/{connection_id}", response_model=ConnectionOut)
    def update_connection(project_id: UUID, connection_id: UUID, data: ConnectionPatch) -> ConnectionOut:
        return execute(
            lambda: ConnectionOut.model_validate(
                gateway._extensions().update_connection(project_id, connection_id, data)
            )
        )

    @router.get("/packages/{package_id}/bindings", response_model=list[ConnectionBindingOut])
    def connection_bindings(project_id: UUID, package_id: UUID) -> list[ConnectionBindingOut]:
        return execute(
            lambda: [
                ConnectionBindingOut.model_validate(item)
                for item in gateway._extensions().list_connection_bindings(project_id, package_id)
            ]
        )

    @router.put("/packages/{package_id}/bindings/{slot}", response_model=ConnectionBindingOut)
    def bind_connection(
        project_id: UUID, package_id: UUID, slot: str, data: ConnectionBindingPut
    ) -> ConnectionBindingOut:
        return execute(
            lambda: ConnectionBindingOut.model_validate(
                gateway._extensions().put_connection_binding(project_id, package_id, slot, data)
            )
        )

    @router.get("/drafts", response_model=list[DraftOut])
    def drafts(project_id: UUID) -> list[DraftOut]:
        return execute(
            lambda: [DraftOut.model_validate(x) for x in gateway._extensions().list_drafts(project_id)]
        )

    @router.get("/drafts/{draft_id}", response_model=DraftOut)
    def draft(project_id: UUID, draft_id: UUID) -> DraftOut:
        return execute(lambda: DraftOut.model_validate(gateway._extensions().get_draft(project_id, draft_id)))

    @router.post("/drafts", response_model=DraftOut, status_code=201)
    def create_draft(project_id: UUID, data: DraftCreate) -> DraftOut:
        return execute(lambda: DraftOut.model_validate(gateway._extensions().create_draft(project_id, data)))

    @router.post("/drafts/{draft_id}/decision", response_model=DraftOut)
    def decide_draft(project_id: UUID, draft_id: UUID, data: DraftDecision) -> DraftOut:
        return execute(
            lambda: DraftOut.model_validate(gateway._extensions().decide_draft(project_id, draft_id, data))
        )

    return router
