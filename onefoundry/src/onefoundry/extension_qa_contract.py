"""Independent checks bound to exact coding bytes, never an Engineer verdict."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from onefoundry.external_code_schemas import ExternalCodeTest
from onefoundry.repository_code_contract import RepositoryCodeManifest, digest
from onefoundry.workflow_schemas import StrictModel


class ExtensionQaJob(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["extension_qa_v1"] = "extension_qa_v1"
    job_id: UUID
    engineer_job: RepositoryCodeManifest
    baseline_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diff_byte_size: int = Field(ge=1, le=65536)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bound_hash(self) -> "ExtensionQaJob":
        if self.request_sha256 != digest(self.model_dump(mode="json", exclude={"request_sha256"})):
            raise ValueError("QA job identity changed")
        return self


class ExtensionQaReceipt(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["extension_qa_receipt_v1"] = "extension_qa_receipt_v1"
    job_id: UUID
    project_id: UUID
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["passed", "failed", "unknown"]
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,159}$")
    tests: list[ExternalCodeTest] = Field(max_length=8)
    observed_at: datetime

    @model_validator(mode="after")
    def actual_checks(self) -> "ExtensionQaReceipt":
        if self.observed_at.tzinfo is None:
            raise ValueError("QA observation requires timezone")
        if self.status == "passed" and (
            not self.tests or any(test.status != "passed" or test.exit_code != 0 for test in self.tests)
        ):
            raise ValueError("passed QA requires observed passing checks")
        return self
