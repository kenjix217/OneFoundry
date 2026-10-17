"""Validated public contracts for organization evidence records."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.workflow_schemas import StrictModel, local_text


class OrganizationCreate(StrictModel):
    display_name: str = Field(min_length=1, max_length=160)
    canonical_domain: str | None = Field(default=None, max_length=253)
    identity_reviewed: bool = False

    @field_validator("display_name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        return local_text(value)

    @field_validator("canonical_domain")
    @classmethod
    def domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in value):
            raise ValueError("canonical domain must be a DNS name")
        if "." not in value or value.startswith(".") or value.endswith(".") or ".." in value:
            raise ValueError("canonical domain must be a DNS name")
        return value

    @model_validator(mode="after")
    def reviewed_requires_domain(self) -> "OrganizationCreate":
        if self.identity_reviewed and self.canonical_domain is None:
            raise ValueError("reviewed identity requires a canonical domain")
        return self


class OrganizationOut(StrictModel):
    id: UUID
    project_id: UUID
    display_name: str
    organization_type: Literal["school_district"]
    canonical_domain: str | None
    identity_reviewed: bool
    qualification: Literal["unreviewed", "qualified", "declined"]
    revision: int
    created_at: datetime


class SourceCreate(StrictModel):
    source_kind: Literal["public_excerpt", "input_file"]
    official_url: str | None = Field(default=None, max_length=2048)
    excerpt: str | None = Field(default=None, max_length=8192)
    input_file_id: UUID | None = None
    observed_at: datetime
    supersedes_source_id: UUID | None = None
    expected_workflow_revision: int = Field(ge=1)

    @field_validator("excerpt")
    @classmethod
    def safe_excerpt(cls, value: str | None) -> str | None:
        return local_text(value) if value is not None else None

    @model_validator(mode="after")
    def evidence_shape(self) -> "SourceCreate":
        if self.source_kind == "public_excerpt":
            if (
                not self.official_url
                or not self.official_url.startswith("https://")
                or not self.excerpt
                or self.input_file_id
            ):
                raise ValueError("public excerpt requires an HTTPS URL and observed excerpt")
            from urllib.parse import urlsplit

            parsed = urlsplit(self.official_url)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.port is not None
                or parsed.fragment
                or parsed.query
            ):
                raise ValueError("invalid official HTTPS URL")
            local_text(self.official_url)
        elif not self.input_file_id or self.official_url or self.excerpt:
            raise ValueError("input-file source requires only a verified input file ID")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at requires a timezone")
        return self


class SourceOut(StrictModel):
    id: UUID
    project_id: UUID
    organization_id: UUID
    source_kind: Literal["public_excerpt", "input_file"]
    official_url: str | None
    excerpt: str | None
    input_file_id: UUID | None
    digest: str
    observed_at: datetime
    revision: int
    superseded_by: UUID | None
    created_at: datetime


class ClaimCreate(StrictModel):
    source_id: UUID
    claim_text: str = Field(min_length=1, max_length=2000)
    locator: str = Field(min_length=1, max_length=500)
    supersedes_claim_id: UUID | None = None
    expected_source_revision: int = Field(ge=1)

    @field_validator("claim_text", "locator")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return local_text(value)


class ClaimReview(StrictModel):
    expected_revision: int = Field(ge=1)
    decision: Literal["approved", "rejected"]


class ClaimOut(StrictModel):
    id: UUID
    project_id: UUID
    organization_id: UUID
    source_id: UUID
    source_revision: int
    claim_text: str
    locator: str
    observed_at: datetime
    review_state: Literal["candidate", "approved", "rejected"]
    verification_state: Literal["not_run", "verified", "failed", "unknown"]
    revision: int
    superseded_by: UUID | None
    fresh: bool
    created_at: datetime


class QualificationChange(StrictModel):
    expected_revision: int = Field(ge=1)
    qualification: Literal["qualified", "declined"]
