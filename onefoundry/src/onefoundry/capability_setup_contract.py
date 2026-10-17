"""Task-bound requests to configure existing tools; proposals grant nothing."""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.workflow_schemas import StrictModel, local_text


class ToolGrant(StrictModel):
    profile_id: UUID
    tool_ids: list[str] = Field(min_length=1, max_length=16)

    @field_validator("tool_ids")
    @classmethod
    def implemented_tools(cls, value: list[str]) -> list[str]:
        from onefoundry.role_tool_admission import registered_tool_ids

        if len(set(value)) != len(value) or set(value) - registered_tool_ids():
            raise ValueError("setup can grant only distinct existing role tools")
        return sorted(value)


class CapabilitySetup(StrictModel):
    kind: Literal["capability_setup_v1"] = "capability_setup_v1"
    grants: list[ToolGrant] = Field(min_length=1, max_length=8)
    source_id: UUID | None = None
    repository_alias: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    verification_path: str | None = Field(default=None, max_length=1024)
    reason: str = Field(min_length=1, max_length=2000)
    checkpoint: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def meaningful_request(self) -> "CapabilitySetup":
        if self.repository_alias is not None and (
            self.source_id is not None
            or self.revision is None
            or self.verification_path is None
            or not any("repository_file_v1" in grant.tool_ids for grant in self.grants)
        ):
            raise ValueError("host repository setup needs its selected revision and verification path")
        return self

    @field_validator("reason", "checkpoint")
    @classmethod
    def safe_text(cls, value: str) -> str:
        value = local_text(value)
        if contains_sensitive_text(value):
            raise ValueError("setup text appears sensitive")
        return value

    @field_validator("verification_path")
    @classmethod
    def safe_path(cls, value: str | None) -> str | None:
        from onefoundry.role_repository_file import _valid_path

        if value is not None and not _valid_path(value):
            raise ValueError("verification path is not admitted")
        return value
