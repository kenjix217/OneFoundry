"""Operator contracts for inert extension packages and reviewed procedures."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from onefoundry_sdk.models import ExtensionManifest
from pydantic import Field, field_validator, model_validator

from onefoundry.workflow_schemas import StrictModel, local_text


class PackageCreate(StrictModel):
    manifest: ExtensionManifest
    files: dict[str, str] = Field(default_factory=dict, max_length=63)


class PackageChange(StrictModel):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_version: str = Field(min_length=5, max_length=80)
    expected_revision: int = Field(ge=1)
    source_run_id: UUID | None = None


class PackageOut(StrictModel):
    id: UUID
    project_id: UUID
    actor_id: str
    package_key: str
    package_version: str
    sha256: str
    state: Literal["draft", "active", "revoked"]
    revision: int
    manifest: dict[str, Any]
    created_at: datetime


class ApprovalPendingOut(StrictModel):
    approval_id: UUID
    status: Literal["pending", "approved", "rejected"]
    review_work_order_id: UUID


class ConnectionConfig(StrictModel):
    transport: Literal["streamable_http", "stdio"] = "streamable_http"
    endpoint: str = Field(default="", max_length=2048)
    auth_method: Literal["none", "bearer", "api_key"] = "none"
    header_name: str | None = Field(default=None, min_length=1, max_length=80)
    stdio_module: str | None = Field(default=None, max_length=200)

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value: str) -> str:
        if any(char.isspace() for char in value) or "@" in value:
            raise ValueError("endpoint must not contain whitespace or userinfo")
        return value

    @model_validator(mode="after")
    def valid_auth(self) -> ConnectionConfig:
        if self.transport == "stdio":
            if self.endpoint or self.auth_method != "none" or self.header_name is not None:
                raise ValueError("stdio connections cannot use an endpoint or authentication")
            if self.stdio_module is None or not re.fullmatch(r"src/[A-Za-z0-9_/-]+\.py", self.stdio_module):
                raise ValueError("stdio module must be a package-owned Python source path")
            if ".." in self.stdio_module.split("/") or "//" in self.stdio_module:
                raise ValueError("stdio module path is unsafe")
            return self
        if not self.endpoint or self.stdio_module is not None:
            raise ValueError("remote connections need an endpoint and cannot set stdio_module")
        if self.auth_method == "api_key":
            if self.header_name is None or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,79}", self.header_name):
                raise ValueError("API key authentication needs a valid header name")
        elif self.header_name is not None:
            raise ValueError("header name is only valid for API key authentication")
        return self


class ConnectionCreate(StrictModel):
    package_id: UUID
    template_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=80)
    config: ConnectionConfig

    @field_validator("label")
    @classmethod
    def safe_label(cls, value: str) -> str:
        return local_text(value)


class ConnectionPatch(StrictModel):
    expected_version: int = Field(ge=1)
    enabled: bool | None = None
    config: ConnectionConfig | None = None

    @model_validator(mode="after")
    def changed(self) -> ConnectionPatch:
        if self.enabled is None and self.config is None:
            raise ValueError("connection update requires enabled or config")
        return self


class ConnectionOut(StrictModel):
    id: UUID
    project_id: UUID
    actor_id: str
    package_id: UUID
    template_id: str
    label: str
    config: ConnectionConfig
    enabled: bool
    version: int
    created_at: datetime


class ConnectionBindingPut(StrictModel):
    connection_id: UUID
    expected_version: int = Field(ge=0)


class ConnectionBindingOut(StrictModel):
    package_id: UUID
    slot: str
    connection_id: UUID
    version: int


class DraftCreate(StrictModel):
    kind: Literal["skill"] = "skill"
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    content: str = Field(min_length=1, max_length=16000)
    required_tools: list[str] = Field(default_factory=list, max_length=32)
    package_id: UUID | None = None
    target_skill_id: UUID | None = None
    expected_base_version: int | None = Field(default=None, ge=1)
    source_run_id: UUID | None = None

    @field_validator("name", "content")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return local_text(value)

    @field_validator("description")
    @classmethod
    def safe_description(cls, value: str) -> str:
        return local_text(value, allow_empty=True)

    @model_validator(mode="after")
    def bounded(self) -> DraftCreate:
        if len(self.content.encode("utf-8")) > 16000:
            raise ValueError("draft content exceeds 16000 UTF-8 bytes")
        if len(set(self.required_tools)) != len(self.required_tools):
            raise ValueError("required tools must be distinct")
        if (self.target_skill_id is None) != (self.expected_base_version is None):
            raise ValueError("existing skill drafts need a skill ID and expected base version")
        return self


class DraftDecision(StrictModel):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["accept", "reject"]


class DraftOut(StrictModel):
    id: UUID
    project_id: UUID
    actor_id: str
    kind: Literal["skill"]
    name: str
    description: str
    content: str
    required_tools: list[str]
    package_id: UUID | None
    target_skill_id: UUID | None
    expected_base_version: int | None
    source_run_id: UUID | None
    sha256: str
    status: Literal["pending", "accepted", "rejected"]
    accepted_skill_id: UUID | None
    accepted_skill_version: int | None
    created_at: datetime


class CatalogOut(StrictModel):
    tools: list[dict[str, Any]]
    packages: list[PackageOut]
    skills: list[dict[str, Any]]
