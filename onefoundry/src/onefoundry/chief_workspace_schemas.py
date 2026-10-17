"""Operator command boundaries; conversation identity survives network interruption."""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.operator_message_contract import MAX_OPERATOR_MESSAGE_CHARS
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.workflow_schemas import StrictModel, local_text


class ChiefMessage(StrictModel):
    client_message_id: UUID
    order_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    expected_sequence: int = Field(default=0, ge=0)
    body: str = Field(min_length=1, max_length=MAX_OPERATOR_MESSAGE_CHARS)
    profile_id: UUID | None = None

    @field_validator("body")
    @classmethod
    def safe_body(cls, value: str) -> str:
        value = local_text(value)
        if contains_sensitive_text(value):
            raise ValueError("message contains sensitive text")
        return value

    @model_validator(mode="after")
    def version_for_existing(self) -> "ChiefMessage":
        if (self.order_id is None) != (self.expected_version is None):
            raise ValueError("existing conversations require their current version")
        if self.order_id is None and self.expected_sequence != 0:
            raise ValueError("new conversations start at sequence zero")
        if self.order_id is not None and self.profile_id is not None:
            raise ValueError(
                "a saved conversation keeps its original agent; select an agent for a new conversation"
            )
        return self


class ChiefReceipt(StrictModel):
    order_id: UUID
    session_id: UUID
    message_id: UUID
    attempt_id: UUID | None
    sequence: int
    disposition: Literal["queued", "coalesced", "discussion", "awaiting_setup"]
    detail: str | None = None


class ChiefControl(StrictModel):
    client_action_id: UUID
    expected_version: int = Field(ge=1)
    expected_controls_version: int = Field(ge=0)
    action: Literal["rename", "pause", "resume", "archive", "restore", "trash"]
    title: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def safe_title(cls, value: str | None) -> str | None:
        if value is not None:
            value = local_text(value)
            if contains_sensitive_text(value):
                raise ValueError("title contains sensitive text")
        return value

    @model_validator(mode="after")
    def rename_requires_title(self) -> "ChiefControl":
        if (self.action == "rename") != (self.title is not None):
            raise ValueError("only rename requires a title")
        return self
