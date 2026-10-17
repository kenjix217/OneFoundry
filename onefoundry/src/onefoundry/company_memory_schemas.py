"""Explicit source capture and reversible memory lifecycle contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.memory_policy import candidate_summary
from onefoundry.memory_schemas import CandidateOut, MemoryScope, ResultOut
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.workflow_schemas import StrictModel


def public_text(value: str) -> str:
    value = candidate_summary(value)
    if contains_sensitive_text(value):
        raise ValueError("company content appears to contain credentials")
    return value


class CapturedMessage(StrictModel):
    speaker: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=4096)
    sent_at: datetime

    _text = field_validator("text")(public_text)

    @field_validator("sent_at")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source timestamps must include a timezone")
        return value


class CaptureCreate(StrictModel):
    client_capture_id: UUID
    source_ref: str = Field(min_length=1, max_length=240)
    authorized_company_content: Literal[True]
    messages: list[CapturedMessage] = Field(min_length=1, max_length=16)
    summary: str = Field(min_length=1, max_length=4096)
    scope: MemoryScope
    replaces_memory_id: UUID | None = None

    _text = field_validator("source_ref", "summary")(public_text)

    @field_validator("authorized_company_content", mode="before")
    @classmethod
    def explicit_authorization(cls, value: object) -> object:
        if value is not True:
            raise ValueError("explicit authorization must be the boolean true")
        return value

    @model_validator(mode="after")
    def bounded_source(self) -> "CaptureCreate":
        if sum(len(message.text.encode()) for message in self.messages) > 16384:
            raise ValueError("captured conversation exceeds the 16384 byte limit")
        return self


class CaptureOut(StrictModel):
    source_id: UUID
    source_sha256: str
    result: ResultOut
    candidate: CandidateOut


class CaptureSourceOut(StrictModel):
    id: UUID
    project_id: UUID
    actor_id: str
    client_capture_id: UUID
    source_ref: str
    messages: list[CapturedMessage]
    source_sha256: str
    payload_sha256: str
    created_at: datetime


class MemoryTransition(StrictModel):
    action: Literal["withdraw", "restore"]
    reason: str = Field(min_length=1, max_length=240)

    _reason = field_validator("reason")(public_text)
