"""Operator-facing outgoing mail contracts; passwords are write-only."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from onefoundry.mail_schemas import HOST
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.workflow_schemas import StrictModel, local_text

ADDRESS = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def address(value: str) -> str:
    value = value.strip()
    if len(value) > 320 or not ADDRESS.fullmatch(value):
        raise ValueError("enter one plain email address")
    return value


class SmtpAccountCreate(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=465, ge=1, le=65535)
    security: Literal["ssl", "starttls"] = "ssl"
    username: str = Field(min_length=1, max_length=320)
    from_address: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=1, max_length=512)

    @field_validator("label")
    @classmethod
    def safe_label(cls, value: str) -> str:
        return local_text(value)

    @field_validator("host")
    @classmethod
    def safe_host(cls, value: str) -> str:
        value = value.strip().lower()
        if not HOST.fullmatch(value):
            raise ValueError("enter an SMTP server name without a scheme or path")
        return value

    @field_validator("username")
    @classmethod
    def safe_username(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char.isspace() for char in value):
            raise ValueError("username must not contain spaces")
        return value

    @field_validator("from_address")
    @classmethod
    def safe_from(cls, value: str) -> str:
        return address(value)


class SmtpAccountOut(StrictModel):
    id: UUID
    label: str
    host: str
    port: int
    security: str
    username: str
    from_address: str
    enabled: bool
    status: str
    last_error_code: str | None
    last_probe_at: datetime | None
    version: int
    created_at: datetime
    credential_present: bool


class SmtpEnabled(StrictModel):
    expected_version: int = Field(ge=1)
    enabled: bool


class OutboundProposal(StrictModel):
    account_id: UUID
    run_id: UUID
    to_address: str = Field(min_length=3, max_length=320)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)

    @field_validator("to_address")
    @classmethod
    def safe_to(cls, value: str) -> str:
        return address(value)

    @field_validator("subject", "body")
    @classmethod
    def safe_content(cls, value: str) -> str:
        return local_text(value)

    @field_validator("body")
    @classmethod
    def bounded_body(cls, value: str) -> str:
        if len(value.encode()) > 12000 or contains_sensitive_text(value):
            raise ValueError("email body is too long or appears to contain a secret")
        return value

    @field_validator("subject")
    @classmethod
    def single_line_subject(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("subject must be one line")
        return value


class OutboundEmailOut(StrictModel):
    id: UUID
    account_id: UUID
    run_id: UUID
    work_order_id: UUID
    approval_id: UUID
    to_address: str
    subject: str
    body: str
    state: str
    reason_code: str | None
    attempted_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
