"""Strict operator contracts for project skill procedures."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from onefoundry.workflow_schemas import local_text


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        return local_text(value)

    @field_validator("description")
    @classmethod
    def safe_description(cls, value: str) -> str:
        return local_text(value, allow_empty=True)


class SkillRename(SkillCreate):
    expected_name: str = Field(min_length=1, max_length=120)
    expected_description: str = Field(max_length=500)

    @field_validator("expected_name")
    @classmethod
    def safe_expected_name(cls, value: str) -> str:
        return local_text(value)

    @field_validator("expected_description")
    @classmethod
    def safe_expected_description(cls, value: str) -> str:
        return local_text(value, allow_empty=True)


class SkillOut(StrictModel):
    id: UUID
    project_id: UUID
    name: str
    description: str
    latest_version: int | None
    created_at: datetime


class SkillVersionCreate(StrictModel):
    content: str = Field(min_length=1, max_length=16000)

    @field_validator("content")
    @classmethod
    def safe_content(cls, value: str) -> str:
        value = local_text(value)
        if len(value.encode("utf-8")) > 16000:
            raise ValueError("skill procedure exceeds 16000 UTF-8 bytes")
        return value


class SkillVersionOut(StrictModel):
    id: UUID
    project_id: UUID
    actor_id: str
    skill_id: UUID
    version: int
    content: str
    sha256: str
    created_at: datetime


class SkillBind(StrictModel):
    version: int = Field(ge=1)
    expected_binding_id: UUID | None


class SkillBindingOut(StrictModel):
    id: UUID
    project_id: UUID
    actor_id: str
    profile_id: UUID
    skill_id: UUID
    skill_name: str
    version: int
    content: str
    sha256: str
    created_at: datetime
