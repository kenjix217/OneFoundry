"""Step-2 project and profile API contracts."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from onefoundry.workflow_schemas import local_text

MemoryScope = Literal["company", "project"]
AutonomyLevel = Literal["manual", "supervised", "bounded"]
ProfileKind = Literal["builtin", "custom"]
MAX_PROFILE_INSTRUCTIONS_CHARS = 50_000
TOOL_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def validate_model(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value or any(char.isspace() for char in value):
        raise ValueError("model must be a nonblank identifier without spaces")
    return value


def validate_tools(value: list[str]) -> list[str]:
    if any(not TOOL_ID.fullmatch(tool) for tool in value) or len(value) != len(set(value)):
        raise ValueError("allowed_tools must contain unique tool identifiers")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class ProjectOut(StrictModel):
    id: UUID
    name: str
    goal: str
    desired_outcome: str
    status: Literal["active", "archived", "trashed"] = "active"
    version: int = Field(default=1, ge=1)
    trash_from_status: Literal["active", "archived"] | None = None
    created_at: datetime


class ProjectTransition(StrictModel):
    expected_version: int = Field(ge=1)


class ProjectBrief(StrictModel):
    goal: str = Field(min_length=1, max_length=4000)
    desired_outcome: str = Field(min_length=1, max_length=4000)
    expected_version: int = Field(ge=1)

    @field_validator("goal", "desired_outcome")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return local_text(value)


class ProjectTrash(ProjectTransition):
    confirm_name: str = Field(min_length=1, max_length=120)


class ProfileFields(StrictModel):
    model: str | None = Field(default=None, max_length=200)
    model_account_id: UUID | None = None
    reasoning_effort: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    instructions: str = Field(default="", max_length=MAX_PROFILE_INSTRUCTIONS_CHARS)
    memory_scope: MemoryScope = "company"
    allowed_tools: list[str] = Field(default_factory=list, max_length=32)
    autonomy_level: AutonomyLevel = "manual"

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str | None) -> str | None:
        return validate_model(value)

    @field_validator("allowed_tools")
    @classmethod
    def valid_tools(cls, value: list[str]) -> list[str]:
        return validate_tools(value)


class ProfileCreate(ProfileFields):
    name: str = Field(min_length=1, max_length=80)
    instructions: str = Field(min_length=1, max_length=MAX_PROFILE_INSTRUCTIONS_CHARS)
    reports_to_profile_id: UUID | None = None

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("instructions")
    @classmethod
    def safe_instructions(cls, value: str) -> str:
        return local_text(value)


class ProfilePatch(StrictModel):
    reports_to_profile_id: UUID | None = None
    model: str | None = Field(default=None, max_length=200)
    model_account_id: UUID | None = None
    reasoning_effort: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    instructions: str | None = Field(default=None, max_length=MAX_PROFILE_INSTRUCTIONS_CHARS)
    memory_scope: MemoryScope | None = None
    allowed_tools: list[str] | None = Field(default=None, max_length=32)
    autonomy_level: AutonomyLevel | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_fields(cls, value: object) -> object:
        if isinstance(value, dict) and any(
            field in value and value[field] is None
            for field in ("instructions", "memory_scope", "allowed_tools", "autonomy_level")
        ):
            raise ValueError("only model may be null")
        return value

    @field_validator("model")
    @classmethod
    def valid_model(cls, value: str | None) -> str | None:
        return validate_model(value)

    @field_validator("allowed_tools")
    @classmethod
    def valid_tools(cls, value: list[str] | None) -> list[str] | None:
        return validate_tools(value) if value is not None else None

    @field_validator("instructions")
    @classmethod
    def valid_instructions(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("instructions cannot be null")
        return local_text(value, allow_empty=True)


class ProfileOut(StrictModel):
    id: UUID
    project_id: UUID
    name: str
    reports_to_profile_id: UUID | None
    kind: ProfileKind
    model: str | None
    model_account_id: UUID | None
    reasoning_effort: str | None
    instructions: str
    memory_scope: MemoryScope
    allowed_tools: list[str]
    autonomy_level: AutonomyLevel
    configured: bool
    permissions_active: Literal[False] = False
