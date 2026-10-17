"""Memory-only MCP inputs; callers cannot choose actor, grants, or review state."""

from uuid import UUID

from pydantic import Field, field_validator

from onefoundry.company_memory_schemas import CaptureCreate
from onefoundry.memory_schemas import MemoryScope, validate_query
from onefoundry.workflow_schemas import StrictModel


class MemorySearch(StrictModel):
    project_id: UUID
    query: str = Field(default="", max_length=120)
    limit: int = Field(default=10, ge=1, le=20, strict=True)
    scope: MemoryScope | None = None

    _query = field_validator("query")(validate_query)


class MemoryRead(StrictModel):
    project_id: UUID
    memory_id: UUID
    scope: MemoryScope | None = None


class MemoryCapture(CaptureCreate):
    project_id: UUID


class MemoryCaptureConfirmed(MemoryCapture):
    preview_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
