"""Approval-bound release identity and observed activation; no runtime paths."""

import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.workflow_schemas import StrictModel, local_text


class ExtensionRelease(StrictModel):
    coding_run_id: UUID
    qa_job_id: UUID
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    app_image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_installed_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    expected_installed_image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    configuration_effects: list[str] = Field(max_length=32)
    migration_effects: list[str] = Field(max_length=32)
    rollback_plan: str = Field(min_length=20, max_length=2000)

    @field_validator("rollback_plan")
    @classmethod
    def safe_text(cls, value: str) -> str:
        local_text(value)
        if contains_sensitive_text(value):
            raise ValueError("activation text appears sensitive")
        return value

    @field_validator("configuration_effects", "migration_effects")
    @classmethod
    def safe_effects(cls, values: list[str]) -> list[str]:
        for value in values:
            if not 1 <= len(value) <= 500:
                raise ValueError("activation effect exceeds its bound")
            cls.safe_text(value)
        return values


class ExtensionToolGrant(StrictModel):
    profile_id: UUID
    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    tools_before: list[str] = Field(max_length=100)


class ExtensionActivationJob(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["extension_activation_v1"] = "extension_activation_v1"
    job_id: UUID
    project_id: UUID
    approval_id: UUID
    target_approval_id: UUID
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qa_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release: ExtensionRelease
    original_work_order_id: UUID
    operation: str = Field(min_length=1, max_length=80)
    gap_source_id: UUID | None
    tool_grants: list[ExtensionToolGrant] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def exact_approval(self) -> "ExtensionActivationJob":
        if self.job_id != self.approval_id:
            raise ValueError("activation job must use the exact approval identity")
        return self


class ExtensionReleaseRecord(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["extension_release_record_v1"] = "extension_release_record_v1"
    project_id: UUID
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qa_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release: ExtensionRelease


class ExtensionActivationReceipt(StrictModel):
    schema_version: Literal[1] = 1
    kind: Literal["extension_activation_receipt_v1"] = "extension_activation_receipt_v1"
    job_id: UUID
    project_id: UUID
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["activated", "failed", "unknown"]
    reason: str = Field(pattern=r"^[a-z][a-z0-9_]{0,159}$")
    installed_version: str | None = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    installed_image: str | None = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    capability_ready: bool
    checkpoint_saved: bool
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("activation observation requires timezone")
        return value

    @model_validator(mode="after")
    def observed_activation(self) -> "ExtensionActivationReceipt":
        if self.status == "activated" and (
            not self.installed_version
            or not self.installed_image
            or not self.capability_ready
            or not self.checkpoint_saved
        ):
            raise ValueError("activation success requires installed identity, capability and checkpoint")
        return self


def approval_matches(job: ExtensionActivationJob, row: dict[str, object]) -> bool:
    from onefoundry.repository_code_contract import digest

    content = row.get("content")
    if not isinstance(content, str):
        return False
    try:
        value = json.loads(content)
    except ValueError:
        return False
    expected = job.model_dump(
        mode="json",
        exclude={"schema_version", "kind", "job_id", "project_id", "approval_id", "proposal_digest"},
    )
    expected["kind"] = "extension_activation_proposal_v1"
    return bool(
        row.get("id") == str(job.approval_id)
        and row.get("project_id") == str(job.project_id)
        and row.get("action_type") == "deploy"
        and row.get("status") == "approved"
        and value == expected
        and row.get("proposal_digest") == job.proposal_digest
        and digest({"action_type": "deploy", "target": row.get("target"), "content": content})
        == job.proposal_digest
    )
