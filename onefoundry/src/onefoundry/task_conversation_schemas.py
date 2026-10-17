"""Explicit task conversation API contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from onefoundry.task_decision_validation import DecisionValidationIssue
from onefoundry.workflow_schemas import StrictModel, local_text


class TurnSend(StrictModel):
    conversation_id: UUID
    client_turn_id: UUID
    expected_sequence: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=1_000_000)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=32)
    discussion_only: bool = Field(default=False, strict=True)

    @field_validator("text")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return local_text(value)


class NewConversation(StrictModel):
    id: UUID
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def safe_title(cls, value: str) -> str:
        safe = local_text(value)
        if not safe.strip():
            raise ValueError("conversation title must not be blank")
        return safe.strip()


class TurnSkillOut(StrictModel):
    skill_id: UUID
    skill_name: str | None = Field(default=None, min_length=1, max_length=120)
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_source: Literal["pinned", "automatic"]


class TurnAttachmentOut(StrictModel):
    id: UUID
    display_name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TurnInputSectionOut(StrictModel):
    file_id: UUID
    file_name: str
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["text", "visual"]
    source_label: str
    chunk_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_count: int = Field(ge=1)


class TurnAssignmentOut(StrictModel):
    id: UUID
    title: str
    status: str
    profile_name: str


class TurnAutomationDecisionOut(StrictModel):
    decision: str
    status: Literal["none", "clarify", "saved", "rejected", "unavailable"]
    rule_id: UUID | None
    rule_version: int | None
    project_version: int | None
    plan_version: int | None
    assigned_work_order_id: UUID | None
    error_code: str | None
    assignments: list[TurnAssignmentOut] = Field(default_factory=list)
    validation_errors: list[DecisionValidationIssue] = Field(default_factory=list)


class ConversationTurnOut(StrictModel):
    id: UUID
    client_turn_id: UUID
    sequence: int
    status: Literal["running", "done", "failed", "unknown"]
    discussion_only: bool = Field(default=False, strict=True)
    source_kind: Literal["operator_ui", "discord", "wechat", "system_triage", "system_followup"] = (
        "operator_ui"
    )
    automation_decision: TurnAutomationDecisionOut | None = None
    operator_text: str
    assistant_text: str | None
    assistant_sha256: str | None
    assistant_actor_id: str | None
    reason: str | None
    diagnostic_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    progress_updates: list[str] = Field(default_factory=list, max_length=8)
    provider: str
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    created_at: datetime
    finished_at: datetime | None
    selected_skills: list[TurnSkillOut] = Field(default_factory=list)
    attachments: list[TurnAttachmentOut] = Field(default_factory=list)
    used_sections: list[TurnInputSectionOut] = Field(default_factory=list)
    input_sections_omitted: int = Field(default=0, ge=0)


class ConversationOut(StrictModel):
    id: UUID
    project_id: UUID
    work_order_id: UUID
    profile_id: UUID
    title: str
    is_active: bool
    status: Literal["open", "busy", "unknown"]
    turns: list[ConversationTurnOut]
    turns_omitted: int = Field(default=0, ge=0)
    latest_sequence: int = Field(default=0, ge=0)


class ConversationSummaryOut(StrictModel):
    id: UUID
    project_id: UUID
    work_order_id: UUID
    profile_id: UUID
    title: str
    is_active: bool
    created_at: datetime
    status: Literal["open", "busy", "unknown"]


class TurnSendOut(StrictModel):
    conversation: ConversationOut
    turn: ConversationTurnOut


class TurnLookupOut(StrictModel):
    conversation_id: UUID
    profile_id: UUID
    turn: ConversationTurnOut
