"""Live text API response contracts; unknown cost is JSON null."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from onefoundry.provider_registry import provider_spec
from onefoundry.task_conversation_schemas import TurnAutomationDecisionOut
from onefoundry.text_role_schemas import SourceMemoryOut
from onefoundry.workflow_schemas import RunOut, StrictModel


class LiveTextOutputOut(StrictModel):
    id: UUID
    project_id: UUID
    run_id: UUID
    profile_id: UUID
    actor_id: str
    account_id: UUID
    provider: str

    @field_validator("provider")
    @classmethod
    def valid_provider(cls, value: str) -> str:
        if provider_spec(value) is None:
            raise ValueError("unknown model provider")
        return value

    model_id: str
    credential_revision: int
    text: str
    text_sha256: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    cost_usd_ticks: int | None
    provider_response_id: str
    source_memory_ids: list[UUID]
    source_memories: list[SourceMemoryOut]
    automation_decision: TurnAutomationDecisionOut | None = None
    created_at: datetime


class LiveTextRunOut(StrictModel):
    run: RunOut
    output: LiveTextOutputOut


class LiveTextRestart(StrictModel):
    prior_run_id: UUID
    expected_version: int = Field(ge=1)
    acknowledge_unknown: Literal[True]
