"""Exact candidate/QA bindings for the separate release packaging worker."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.extension_activation_contract import ExtensionRelease, ExtensionReleaseRecord
from onefoundry.extension_qa_contract import ExtensionQaJob
from onefoundry.repository_code_contract import digest
from onefoundry.workflow_schemas import StrictModel


class PrepareRelease(StrictModel):
    coding_run_id: UUID
    qa_job_id: UUID
    configuration_effects: list[str] = Field(max_length=32)
    migration_effects: list[str] = Field(max_length=32)
    rollback_plan: str = Field(min_length=20, max_length=2000)

    @field_validator("rollback_plan")
    @classmethod
    def safe_text(cls, value: str) -> str:
        return ExtensionRelease.safe_text(value)

    @field_validator("configuration_effects", "migration_effects")
    @classmethod
    def safe_effects(cls, values: list[str]) -> list[str]:
        return ExtensionRelease.safe_effects(values)


class ReleasePreparationJob(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["extension_release_prepare_v1"] = "extension_release_prepare_v1"
    job_id: UUID
    project_id: UUID
    session_id: UUID
    work_order_id: UUID
    qa_job: ExtensionQaJob
    qa_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: PrepareRelease
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_identity(self) -> "ReleasePreparationJob":
        if (
            self.project_id != self.qa_job.engineer_job.project_id
            or self.request.coding_run_id != self.qa_job.engineer_job.run_id
            or self.request.qa_job_id != self.qa_job.job_id
            or self.request_sha256 != digest(self.model_dump(mode="json", exclude={"request_sha256"}))
        ):
            raise ValueError("release preparation identity changed")
        return self


class ReleasePreparationReceipt(StrictModel):
    kind: Literal["extension_release_prepare_receipt_v1"] = "extension_release_prepare_receipt_v1"
    job_id: UUID
    project_id: UUID
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["prepared", "failed", "unknown"]
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,159}$")
    record: ExtensionReleaseRecord | None = None
    observed_at: datetime

    @model_validator(mode="after")
    def observed_identity(self) -> "ReleasePreparationReceipt":
        if self.observed_at.tzinfo is None or (self.status == "prepared") != (self.record is not None):
            raise ValueError("prepared release requires an exact observed record")
        if self.record is not None and self.record.project_id != self.project_id:
            raise ValueError("prepared release project differs")
        return self
