"""Bounded Zoho Mail inputs; the private endpoint and OAuth token are write-only."""

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.smtp_schemas import address
from onefoundry.workflow_schemas import StrictModel, local_text

ReadOperation = Literal["connections", "folders", "list", "search", "content", "details", "headers"]
WriteOperation = Literal["draft", "send", "reply_draft", "reply_send"]


def endpoint(value: str) -> str:
    """Only the vendor's hosted HTTPS endpoint; no proxy, query or userinfo."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not re.fullmatch(r"[a-z0-9-]+\.zohomcp\.com", parsed.hostname)
        or parsed.netloc != parsed.hostname
        or not re.fullmatch(r"/mcp/[a-f0-9]{32}/message", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("enter the complete Zoho-hosted MCP HTTPS URL")
    return value


class ZohoSetup(StrictModel):
    label: str = Field(min_length=1, max_length=80)

    @field_validator("label")
    @classmethod
    def safe_label(cls, value: str) -> str:
        return local_text(value)


class ZohoCredentials(StrictModel):
    expected_version: int = Field(ge=1)
    server_url: SecretStr = Field(min_length=1, max_length=2048)
    access_token: SecretStr = Field(min_length=1, max_length=4096)
    mail_account_id: str = Field(default="", pattern=r"^[0-9]{0,30}$")

    @model_validator(mode="after")
    def valid_credentials(self) -> "ZohoCredentials":
        endpoint(self.server_url.get_secret_value())
        token = self.access_token.get_secret_value()
        if any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError("enter an OAuth access token without spaces")
        return self


class ZohoEnabled(StrictModel):
    expected_version: int = Field(ge=1)
    enabled: bool


class ZohoEndpoint(StrictModel):
    expected_version: int = Field(ge=1)
    server_url: SecretStr = Field(min_length=1, max_length=2048)
    mail_account_id: str = Field(default="", pattern=r"^[0-9]{0,30}$")

    @model_validator(mode="after")
    def valid_endpoint(self) -> "ZohoEndpoint":
        endpoint(self.server_url.get_secret_value())
        return self


class ZohoSignIn(StrictModel):
    expected_version: int = Field(ge=1)


class ZohoSignInOut(StrictModel):
    authorize_path: str


class ZohoConnectionOut(StrictModel):
    id: UUID
    label: str
    status: Literal["needs_credentials", "unverified", "ready", "failed"]
    enabled: bool
    credential_present: bool
    endpoint_present: bool = False
    oauth_pending: bool = False
    mail_account_id: str | None
    from_address: str | None
    last_error_code: str | None
    last_probe_at: datetime | None
    version: int
    created_at: datetime


class ZohoRead(StrictModel):
    connection_id: UUID | None = None
    operation: ReadOperation
    folder_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    message_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    search_key: str | None = Field(default=None, min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def valid_operation(self) -> "ZohoRead":
        if self.operation != "connections" and self.connection_id is None:
            raise ValueError("mail reading requires a selected connection")
        if self.operation in {"content", "details", "headers"} and (
            not self.folder_id or not self.message_id
        ):
            raise ValueError("message reading requires folder and message identifiers")
        if self.operation == "search" and not self.search_key:
            raise ValueError("search requires Zoho search syntax")
        if self.search_key and contains_sensitive_text(self.search_key):
            raise ValueError("search must not contain credentials")
        return self


class ZohoWrite(StrictModel):
    connection_id: UUID
    operation: WriteOperation
    to_address: str = Field(min_length=3, max_length=320)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    message_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")

    @field_validator("to_address")
    @classmethod
    def safe_recipient(cls, value: str) -> str:
        return address(value)

    @field_validator("subject", "body")
    @classmethod
    def safe_content(cls, value: str) -> str:
        value = local_text(value)
        if len(value.encode()) > 12000 or contains_sensitive_text(value):
            raise ValueError("mail content is too long or appears to contain credentials")
        return value

    @model_validator(mode="after")
    def valid_operation(self) -> "ZohoWrite":
        if "\r" in self.subject or "\n" in self.subject:
            raise ValueError("subject must be one line")
        if self.operation.startswith("reply_") and not self.message_id:
            raise ValueError("a reply requires the original message identifier")
        if not self.operation.startswith("reply_") and self.message_id:
            raise ValueError("only replies take a message identifier")
        return self


class ZohoManualProposal(ZohoWrite):
    run_id: UUID
