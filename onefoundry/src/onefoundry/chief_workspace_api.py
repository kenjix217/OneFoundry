"""Thin project-scoped command routes; workers continue to own model execution."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Response

from onefoundry import chief_workspace_messages, chief_workspace_reads
from onefoundry.chief_workspace_controls import control
from onefoundry.chief_workspace_schemas import ChiefControl, ChiefMessage, ChiefReceipt
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_chief_workspace_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/chief-workspace")

    @router.get("")
    def conversations(project_id: UUID) -> list[dict[str, Any]]:
        return execute(lambda: chief_workspace_reads.list_conversations(gateway, project_id))

    @router.get("/resolve/{order_id}")
    def resolve(project_id: UUID, order_id: UUID) -> dict[str, Any]:
        return execute(lambda: chief_workspace_reads.resolve_conversation(gateway, project_id, order_id))

    @router.post("/messages", response_model=ChiefReceipt)
    def message(project_id: UUID, data: ChiefMessage, response: Response) -> ChiefReceipt:
        receipt, created = execute(lambda: chief_workspace_messages.send(gateway, project_id, data))
        response.status_code = 202 if created else 200
        return receipt

    @router.get("/messages/{client_message_id}", response_model=ChiefReceipt)
    def lookup(project_id: UUID, client_message_id: UUID) -> ChiefReceipt:
        return execute(lambda: chief_workspace_messages.lookup(gateway, project_id, client_message_id))

    @router.post("/{order_id}/controls")
    def controls(project_id: UUID, order_id: UUID, data: ChiefControl) -> dict[str, Any]:
        return execute(lambda: control(gateway, project_id, order_id, data))

    return router
