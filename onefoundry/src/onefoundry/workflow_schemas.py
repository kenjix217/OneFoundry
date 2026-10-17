"""Step-3 manual workflow request and response contracts."""

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

WorkOrderStatus = Literal["draft", "ready", "running", "blocked", "needs_review", "done", "failed", "unknown"]
DeliverableKind = Literal[
    "text_reply", "artifact", "document", "code_change", "external_action", "operator_review"
]
RunStatus = Literal["running", "done", "failed", "unknown"]
ApprovalStatus = Literal["pending", "approved", "rejected"]
ActionType = Literal["email", "publish", "merge", "deploy", "cloud_change", "other"]
SECRET_PATTERN = re.compile(
    r"(?:\b(?:sk-[A-Za-z0-9_-]{12,}|sk_live_[A-Za-z0-9]{12,}|"
    r"ghp_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
    r"xoxb-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*\S+|"
    r"\bBearer\s+[A-Za-z0-9._~-]{12,})",
    re.IGNORECASE,
)


def local_text(value: str, *, allow_empty: bool = False) -> str:
    value = value.strip()
    if not value and not allow_empty:
        raise ValueError("value must not be blank")
    if SECRET_PATTERN.search(value):
        raise ValueError("text appears to contain a secret")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkOrderCreate(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    instructions: str = Field(min_length=1, max_length=16000)
    profile_id: UUID
    deliverable_kind: DeliverableKind = "operator_review"
    acceptance_checks: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("title", "instructions")
    @classmethod
    def nonblank(cls, value: str) -> str:
        return local_text(value)

    @field_validator("acceptance_checks")
    @classmethod
    def valid_checks(cls, value: list[str]) -> list[str]:
        if any(len(item) > 300 for item in value):
            raise ValueError("acceptance check is too long")
        return [local_text(item) for item in value]


class WorkOrderUpdate(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    instructions: str = Field(min_length=1, max_length=16000)
    expected_version: int = Field(ge=1)

    @field_validator("title", "instructions")
    @classmethod
    def nonblank(cls, value: str) -> str:
        return local_text(value)


class WorkOrderTrigger(StrictModel):
    signal_id: UUID
    signal_type: str
    source: str
    observed_at: datetime
    rule_name: str | None
    rule_version: int | None


class WorkOrderOut(StrictModel):
    id: UUID
    project_id: UUID
    title: str
    instructions: str
    profile_id: UUID
    deliverable_kind: DeliverableKind
    acceptance_checks: list[str]
    parent_work_order_id: UUID | None
    source_run_id: UUID | None
    origin_signal_id: UUID | None = None
    chain_depth: int = 0
    triggered_by: WorkOrderTrigger | None = None
    status: WorkOrderStatus
    version: int
    block_reason: str | None
    blocked_from_status: Literal["draft", "ready", "running"] | None
    created_at: datetime


class WorkOrderAssign(StrictModel):
    profile_id: UUID
    expected_version: int = Field(ge=1)


class WorkOrderBlock(StrictModel):
    reason: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        return local_text(value)


class WorkOrderUnblock(StrictModel):
    expected_version: int = Field(ge=1)


class WorkOrderReview(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        return local_text(value)


class HandoffCreate(StrictModel):
    profile_id: UUID


class FinishCreate(StrictModel):
    outcome: Literal["done", "failed", "unknown"]
    reason: str = Field(default="", max_length=2000)

    @field_validator("reason")
    @classmethod
    def safe_reason(cls, value: str) -> str:
        return local_text(value, allow_empty=True)


class RunOut(StrictModel):
    id: UUID
    project_id: UUID
    work_order_id: UUID
    profile_id: UUID
    work_order_title: str | None
    work_order_instructions: str | None
    profile_model: str | None
    status: RunStatus
    mode: Literal["manual_planning", "synthetic_text", "synthetic_code", "live_text", "external_code"]
    tokens_used: int | None
    cost_usd: float | None
    outcome_reason: str
    started_at: datetime
    finished_at: datetime | None
    triggered_by: WorkOrderTrigger | None = None


class EventOut(StrictModel):
    id: UUID
    project_id: UUID
    work_order_id: UUID | None
    run_id: UUID | None
    actor_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class ProfileSnapshotOut(StrictModel):
    profile_id: UUID
    profile_name: str
    profile_model: str | None
    profile_reasoning_effort: str | None
    profile_instructions: str
    memory_scope: Literal["company", "project"]
    allowed_tools: list[str]
    autonomy_level: Literal["manual", "supervised", "bounded"]
    transition_index: int
    actor_id: str
    recorded_at: datetime


class RunSkillSnapshotOut(StrictModel):
    skill_id: UUID
    skill_name: str
    version: int
    sha256: str
    transition_index: int
    actor_id: str
    created_at: datetime


class RunDetail(RunOut):
    events: list[EventOut]
    profile_snapshots: list[ProfileSnapshotOut]
    skill_snapshots: list[RunSkillSnapshotOut]


class ApprovalCreate(StrictModel):
    action_type: ActionType
    target: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=16000)

    @field_validator("target", "content")
    @classmethod
    def nonblank_without_obvious_secret(cls, value: str) -> str:
        return local_text(value)


class ApprovalDecision(StrictModel):
    decision: Literal["approved", "rejected"]


class ApprovalOut(StrictModel):
    id: UUID
    project_id: UUID
    work_order_id: UUID
    run_id: UUID
    action_type: ActionType
    target: str
    content: str
    proposal_digest: str
    status: ApprovalStatus
    created_at: datetime
    decided_at: datetime | None
    synthetic_merge: bool = False
    synthetic_sales: bool = False
    outbound_email: bool = False
    zoho_mail_operation: Literal["draft", "send", "reply_draft", "reply_send"] | None = None
    github_stage: bool = False
    github_fetch: bool = False
    github_snapshot: bool = False
    github_comment: bool = False
