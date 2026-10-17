"""Explicit task conversation HTTP surface; reads never call a provider."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse

from onefoundry.task_conversation_schemas import (
    ConversationOut,
    ConversationSummaryOut,
    NewConversation,
    TurnLookupOut,
    TurnSend,
    TurnSendOut,
)
from onefoundry.task_conversation_store import ConversationFailed, ConversationUnknown
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_task_conversation_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/work-orders/{order_id}")

    @router.get("/conversation", response_model=ConversationOut)
    def get(project_id: UUID, order_id: UUID) -> ConversationOut:
        return ConversationOut.model_validate(
            execute(lambda: gateway.get_task_conversation(project_id, order_id))
        )

    @router.post("/conversation", response_model=ConversationOut)
    def open_conversation(project_id: UUID, order_id: UUID, response: Response) -> ConversationOut:
        saved, created = execute(lambda: gateway.open_task_conversation(project_id, order_id))
        response.status_code = 201 if created else 200
        return ConversationOut.model_validate(saved)

    @router.post("/conversation/turns/{turn_id}/acknowledge-unknown", response_model=ConversationOut)
    def acknowledge_unknown(project_id: UUID, order_id: UUID, turn_id: UUID) -> ConversationOut:
        return ConversationOut.model_validate(
            execute(lambda: gateway.acknowledge_unknown_task_conversation_turn(project_id, order_id, turn_id))
        )

    @router.post("/conversation/turns", response_model=TurnSendOut)
    def send(
        project_id: UUID, order_id: UUID, data: TurnSend, response: Response
    ) -> TurnSendOut | JSONResponse:
        try:
            saved, created = execute(lambda: gateway.send_task_conversation(project_id, order_id, data))
        except ConversationFailed as exc:
            return JSONResponse(
                status_code=502,
                content={"detail": "agent provider rejected the turn", "turn_id": str(exc.turn_id)},
            )
        except ConversationUnknown as exc:
            return JSONResponse(
                status_code=503,
                content={"detail": "agent response outcome unknown", "turn_id": str(exc.turn_id)},
            )
        response.status_code = 201 if created else 200
        return TurnSendOut.model_validate(saved)

    @router.get("/conversation/turns/by-client/{client_turn_id}", response_model=TurnLookupOut)
    def lookup(project_id: UUID, order_id: UUID, client_turn_id: UUID) -> TurnLookupOut:
        return TurnLookupOut.model_validate(
            execute(lambda: gateway.lookup_task_conversation_turn(project_id, order_id, client_turn_id))
        )

    @router.get("/conversations", response_model=list[ConversationSummaryOut])
    def list_conversations(project_id: UUID, order_id: UUID) -> list[ConversationSummaryOut]:
        rows = execute(lambda: gateway.list_task_conversations(project_id, order_id))
        return [ConversationSummaryOut.model_validate(row) for row in rows]

    @router.post("/conversations", response_model=ConversationOut)
    def new_conversation(
        project_id: UUID, order_id: UUID, data: NewConversation, response: Response
    ) -> ConversationOut:
        saved, created = execute(lambda: gateway.new_task_conversation(project_id, order_id, data))
        response.status_code = 201 if created else 200
        return ConversationOut.model_validate(saved)

    @router.get("/conversations/{conversation_id}", response_model=ConversationOut)
    def get_by_id(
        project_id: UUID,
        order_id: UUID,
        conversation_id: UUID,
        before_sequence: Annotated[int | None, Query(ge=1)] = None,
    ) -> ConversationOut:
        return ConversationOut.model_validate(
            execute(
                lambda: gateway.get_task_conversation_by_id(
                    project_id, order_id, conversation_id, before_sequence
                )
            )
        )

    return router
