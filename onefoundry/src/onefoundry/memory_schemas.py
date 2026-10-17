"""Bounded local result, DreamCycle review, and memory API contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from onefoundry.memory_policy import candidate_summary, query_terms
from onefoundry.workflow_schemas import StrictModel, local_text

MemoryScope = Literal["project", "company"]
CandidateStatus = Literal["pending", "approved", "rejected"]


class ResultCreate(StrictModel):
    summary: str = Field(min_length=1, max_length=4096)
    scope: MemoryScope

    @field_validator("summary")
    @classmethod
    def safe_summary(cls, value: str) -> str:
        return candidate_summary(value)


class ResultOut(StrictModel):
    id: UUID
    project_id: UUID
    actor_id: str
    run_id: UUID | None
    summary: str
    scope: MemoryScope
    source_kind: Literal["operator_entered", "synthetic_sales", "synthetic_qa", "conversation_capture"]
    source_capture_id: UUID | None = None
    source_sales_draft_id: UUID | None = None
    source_runner_job_id: UUID | None = None
    source_sha256: str | None = None
    created_at: datetime


class SyntheticProposalCreate(StrictModel):
    scope: MemoryScope


class CandidateOut(StrictModel):
    id: UUID
    project_id: UUID
    actor_id: str
    source_result_id: UUID
    summary: str
    scope: MemoryScope
    status: CandidateStatus
    created_at: datetime
    reviewed_at: datetime | None
    reviewed_by_actor_id: str | None
    replaces_memory_id: UUID | None = None
    source_capture_id: UUID | None = None


class SyntheticProposalOut(StrictModel):
    result: ResultOut
    candidate: CandidateOut


class CandidateReview(StrictModel):
    decision: Literal["approved", "rejected"]


class MemoryOut(StrictModel):
    id: UUID
    project_id: UUID
    actor_id: str
    scope: MemoryScope
    text: str
    source_result_id: UUID
    source_candidate_id: UUID
    created_at: datetime
    score: int
    similarity: float | None = Field(
        default=None, ge=-1.000001, le=1.000001, allow_inf_nan=False, exclude_if=lambda value: value is None
    )
    state: Literal["current", "withdrawn", "superseded"] = "current"
    superseded_by: UUID | None = None
    changed_at: datetime | None = None
    changed_by_actor_id: str | None = None


def validate_query(value: str) -> str:
    query_terms(value)
    return local_text(value, allow_empty=True)
