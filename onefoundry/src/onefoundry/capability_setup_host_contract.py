"""Named host repository offers and task-bound setup receipts contain no host paths."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator

from onefoundry.workflow_schemas import StrictModel


class RepositoryOffer(StrictModel):
    alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    project_id: UUID
    branch: str = Field(min_length=1, max_length=200)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    path_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HostSetupCatalog(StrictModel):
    kind: Literal["capability_setup_catalog_v1"] = "capability_setup_catalog_v1"
    observed_at: datetime
    repositories: list[RepositoryOffer] = Field(max_length=32)

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("host observation needs a timezone")
        return value


class HostSetupJob(StrictModel):
    kind: Literal["capability_setup_host_v1"] = "capability_setup_host_v1"
    job_id: UUID
    project_id: UUID
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding: dict[str, Any]
    target: str
    content: str


class HostSetupReceipt(StrictModel):
    kind: Literal["capability_setup_host_receipt_v1"] = "capability_setup_host_receipt_v1"
    job_id: UUID
    project_id: UUID
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["configured", "failed", "unknown"]
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,100}$")
    source_id: UUID | None = None
