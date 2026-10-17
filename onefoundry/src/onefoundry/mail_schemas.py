"""Operator contracts for read-only mailbox connections; passwords are write-only."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator

from onefoundry.workflow_schemas import StrictModel, local_text

HOST = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


class MailAccountCreate(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=993, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=320)
    folder: str = Field(default="INBOX", min_length=1, max_length=200)
    password: SecretStr = Field(min_length=1, max_length=512)

    @field_validator("label", "folder")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return local_text(value)

    @field_validator("host")
    @classmethod
    def plain_host(cls, value: str) -> str:
        value = value.strip().lower()
        if not HOST.match(value):
            raise ValueError("enter a server name such as imap.zoho.com, without a scheme or path")
        return value

    @field_validator("username")
    @classmethod
    def plain_username(cls, value: str) -> str:
        value = value.strip()
        if not value or any(char.isspace() for char in value):
            raise ValueError("username must not contain spaces")
        return value


class MailApiAccountCreate(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    protocol: Literal["gmail", "graph"]
    connector_account_id: UUID

    @field_validator("label")
    @classmethod
    def safe_label(cls, value: str) -> str:
        return local_text(value)


class MailVersion(StrictModel):
    expected_version: int = Field(ge=1)


class MailEnabled(MailVersion):
    enabled: bool


class MailAccountOut(StrictModel):
    id: UUID
    label: str
    protocol: str
    host: str | None
    port: int | None
    username: str | None
    folder: str
    enabled: bool
    status: str
    last_error_code: str | None
    capabilities: list[str]
    last_probe_at: datetime | None
    last_poll_at: datetime | None
    version: int
    created_at: datetime
    credential_present: bool
    connector_account_id: UUID | None
