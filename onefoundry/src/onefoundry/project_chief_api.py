"""Read and send the operator's project Chief conversation."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import Field, field_validator

from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute
from onefoundry.workflow_schemas import StrictModel, local_text


class ChiefTurnSend(StrictModel):
    client_turn_id: UUID
    text: str = Field(min_length=1, max_length=16000)

    @field_validator("text")
    @classmethod
    def _safe(cls, value: str) -> str:
        return local_text(value)


class ChiefTurnOut(StrictModel):
    id: UUID
    project_id: UUID
    client_turn_id: UUID
    source_kind: Literal["operator_ui", "discord", "wechat"]
    status: Literal["pending", "running", "done", "failed", "unknown", "blocked"]
    operator_text: str
    assistant_text: str | None
    reason: str | None
    action: str | None
    target_work_order_id: UUID | None
    created_work_order_id: UUID | None
    created_rule_id: UUID | None
    remote_inbound_id: UUID | None = Field(default=None, exclude=True)
    created_at: datetime
    finished_at: datetime | None


def make_project_chief_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/chief-inbox")

    @router.get("/turns", response_model=list[ChiefTurnOut])
    def list_turns(project_id: UUID) -> list[ChiefTurnOut]:
        return [
            ChiefTurnOut.model_validate(row)
            for row in execute(lambda: gateway.list_project_chief_turns(project_id))
        ]

    @router.post("/turns", response_model=ChiefTurnOut)
    def send(project_id: UUID, data: ChiefTurnSend, response: Response) -> ChiefTurnOut:
        row = execute(lambda: gateway.enqueue_project_chief_turn(project_id, data.client_turn_id, data.text))
        response.status_code = 202
        return ChiefTurnOut.model_validate(row)

    return router
