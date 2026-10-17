"""Public contracts for project repository sources and GitHub accounts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from onefoundry.source_github import BRANCH, REPO_PART


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRootOut(StrictModel):
    id: Literal["workspace"]
    label: str
    available: bool


class LocalRepositoryOut(StrictModel):
    relative_path: str
    display_name: str
    branch: str
    revision: str
    dirty: bool


class LocalRepositoryDiscovery(StrictModel):
    available: bool
    truncated: bool
    repositories: list[LocalRepositoryOut]


class LocalSourceCreate(StrictModel):
    root_id: Literal["workspace"]
    relative_path: str = Field(min_length=1, max_length=500)

    @field_validator("relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if value == ".":
            return value
        parts = value.split("/")
        if value.startswith("/") or any(part in {"", ".", ".."} for part in parts) or "\\" in value:
            raise ValueError("relative_path must stay inside the configured source root")
        if any(ord(char) < 32 for char in value):
            raise ValueError("relative_path contains control characters")
        return value


class GitHubAccountCreate(StrictModel):
    label: str = Field(min_length=1, max_length=120)
    token: SecretStr

    @field_validator("label")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("label must not be blank")
        return value


class GitHubAccountOut(StrictModel):
    id: UUID
    project_id: UUID
    label: str
    credential_present: bool


class GitHubRepoOut(StrictModel):
    full_name: str
    default_branch: str
    private: bool
    html_url: str


class GitHubSourceCreate(StrictModel):
    account_id: UUID
    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)
    branch: str | None = Field(default=None, max_length=200)

    @field_validator("owner", "repo")
    @classmethod
    def valid_component(cls, value: str) -> str:
        if not REPO_PART.fullmatch(value):
            raise ValueError("invalid GitHub repository identifier")
        return value

    @field_validator("branch")
    @classmethod
    def valid_branch(cls, value: str | None) -> str | None:
        if value is not None and not BRANCH.fullmatch(value):
            raise ValueError("invalid GitHub branch")
        return value


class SourceOut(StrictModel):
    id: UUID
    project_id: UUID
    kind: Literal["local", "github"]
    display_name: str
    root_id: str | None
    relative_path: str | None
    account_id: UUID | None
    owner: str | None
    repo: str | None
    branch: str
    revision: str
    availability: Literal["verified_at_selection", "unavailable"]
    created_at: datetime
