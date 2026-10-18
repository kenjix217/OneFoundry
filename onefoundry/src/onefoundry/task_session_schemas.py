"""Read contracts for continuing work-order team sessions."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from onefoundry.operator_message_contract import MAX_OPERATOR_MESSAGE_CHARS
from onefoundry.workflow_schemas import StrictModel, WorkOrderStatus, local_text


class SessionMessageCreate(StrictModel):
    client_message_id: UUID
    expected_sequence: int = Field(ge=0)
    body: str = Field(min_length=1, max_length=MAX_OPERATOR_MESSAGE_CHARS)

    @field_validator("body")
    @classmethod
    def safe_body(cls, value: str) -> str:
        return local_text(value)


class SessionContinueCreate(StrictModel):
    client_attempt_id: UUID


class SessionEnqueueOut(StrictModel):
    session_id: UUID
    message_id: UUID | None
    attempt_id: UUID | None
    sequence: int
    state: Literal["queued", "coalesced", "waiting_for_input"]


class SessionOut(StrictModel):
    waiting_reason: Literal["local_recording", "recording_ready", "recording_unconfirmed"] | None = None
    id: UUID
    project_id: UUID
    actor_id: str
    work_order_id: UUID
    root_work_order_id: UUID
    profile_id: UUID
    parent_session_id: UUID | None
    kind: Literal["main", "child", "consultation"]
    is_order_owner: bool
    status: Literal["open", "working", "waiting_for_agent", "waiting_for_operator", "closed"]
    execution_mode: Literal["legacy", "team"]
    work_order_status: WorkOrderStatus | None = None
    provider_thread_id: str | None = None
    created_at: datetime


class SessionMessageOut(StrictModel):
    id: UUID
    project_id: UUID
    session_id: UUID
    source_session_id: UUID | None
    source_turn_id: UUID | None
    client_message_id: UUID | None
    sequence: int = Field(ge=1)
    actor_id: str
    kind: Literal["operator", "agent", "system", "legacy_operator", "legacy_agent"]
    body: str
    created_at: datetime


class SessionAttemptOut(StrictModel):
    purpose: Literal["work", "discussion"] = "work"
    unknown_acknowledged: bool = False
    terminal_result_reconciled: bool = False
    id: UUID
    project_id: UUID
    session_id: UUID
    source_message_id: UUID | None
    client_attempt_id: UUID | None = None
    input_sequence: int | None = None
    status: Literal["queued", "running", "done", "failed", "unknown"]
    provider: str | None
    model_id: str | None
    provider_thread_id: str | None
    provider_turn_id: str | None
    prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_message_ids: list[UUID]
    context_dependency_ids: list[UUID]
    context_omitted_count: int = Field(ge=0)
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    reason: str | None
    retry_of_attempt_id: UUID | None = None
    not_before: datetime | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class SessionChildOut(StrictModel):
    execution_mode: Literal["legacy", "team"] = "legacy"
    work_order_status: WorkOrderStatus | None = None
    id: UUID
    project_id: UUID
    work_order_id: UUID
    root_work_order_id: UUID
    profile_id: UUID
    parent_session_id: UUID | None
    kind: Literal["main", "child", "consultation"]
    is_order_owner: bool
    status: Literal["open", "working", "waiting_for_agent", "waiting_for_operator", "closed"]
    created_at: datetime


class SessionAssignmentOut(StrictModel):
    id: UUID
    project_id: UUID
    root_work_order_id: UUID
    parent_session_id: UUID
    child_session_id: UUID
    plan_revision: int = Field(ge=1)
    objective: str
    expected_result: str
    acceptance_check: str
    acceptance_kind: Literal["text_report", "artifact", "operator_review", "qa_review"]
    status: Literal["pending", "ready", "active", "satisfied", "blocked", "canceled"]
    result_message_id: UUID | None
    created_at: datetime


class SessionDependencyOut(StrictModel):
    assignment_id: UUID
    depends_on_id: UUID


class SessionDecisionCauseOut(StrictModel):
    id: UUID
    main_session_id: UUID
    plan_revision: int = Field(ge=1)
    kind: Literal["blocker", "synthesis"]
    detail: str
    dispatch_attempt_id: UUID | None
    resolved_at: datetime | None
    handled_at: datetime | None
    created_at: datetime


class SessionToolCallOut(StrictModel):
    id: UUID
    session_id: UUID
    attempt_id: UUID
    tool_name: str
    status: Literal["started", "done", "failed"]
    result: dict[str, object] | None
    reason: str | None
    created_at: datetime
    finished_at: datetime | None


class SessionHistoryOmitted(StrictModel):
    messages: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0)
    decision_causes: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


class SessionViewOut(StrictModel):
    session: SessionOut
    messages: list[SessionMessageOut]
    attempts: list[SessionAttemptOut]
    children: list[SessionChildOut]
    assignments: list[SessionAssignmentOut]
    dependencies: list[SessionDependencyOut]
    decision_causes: list[SessionDecisionCauseOut]
    tool_calls: list[SessionToolCallOut]
    history_omitted: SessionHistoryOmitted = Field(default_factory=SessionHistoryOmitted)
