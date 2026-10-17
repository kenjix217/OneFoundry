"""Secret-free contract shared by the offline GitHub stage dispatcher and supervisor."""

import hashlib
import json
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from onefoundry.source_github import BRANCH, REPO_PART, SHA

SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError):
        return False


class ExactSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["github_stage_v1"]
    project_id: str
    source_id: str
    account_id: str
    owner: str
    repo: str
    branch: str
    saved_revision: str

    @model_validator(mode="after")
    def exact(self) -> "ExactSource":
        if (
            not all(_uuid(value) for value in (self.project_id, self.source_id, self.account_id))
            or REPO_PART.fullmatch(self.owner) is None
            or REPO_PART.fullmatch(self.repo) is None
            or BRANCH.fullmatch(self.branch) is None
            or SHA.fullmatch(self.saved_revision) is None
        ):
            raise ValueError("invalid exact source")
        return self


class StageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    target_id: Literal["github_stage_offline_v1"]
    job_id: str
    project_id: str
    run_id: str
    approval_id: str
    actor_id: str = Field(min_length=1, max_length=128)
    proposal_digest: str
    source: ExactSource
    request_sha256: str

    @model_validator(mode="after")
    def exact(self) -> "StageManifest":
        if (
            not all(_uuid(value) for value in (self.job_id, self.project_id, self.run_id, self.approval_id))
            or self.source.project_id != self.project_id
            or SHA256.fullmatch(self.proposal_digest) is None
            or SHA256.fullmatch(self.request_sha256) is None
            or self.request_sha256 != digest(self.model_dump(exclude={"request_sha256"}))
        ):
            raise ValueError("invalid GitHub stage manifest binding")
        return self


class StageReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    target_id: Literal["github_stage_offline_v1"]
    job_id: str
    project_id: str
    run_id: str
    approval_id: str
    proposal_digest: str
    source: ExactSource
    request_sha256: str
    status: Literal["done", "unknown"]
    reason_code: str = Field(min_length=1, max_length=80)
    source_tree: str | None
    file_count: int | None
    total_bytes: int | None
    started_at: str
    finished_at: str

    @model_validator(mode="after")
    def exact(self) -> "StageReceipt":
        if (
            not all(_uuid(value) for value in (self.job_id, self.project_id, self.run_id, self.approval_id))
            or self.source.project_id != self.project_id
            or SHA256.fullmatch(self.proposal_digest) is None
            or SHA256.fullmatch(self.request_sha256) is None
            or any(ord(char) < 32 or ord(char) > 126 for char in self.reason_code)
        ):
            raise ValueError("invalid GitHub stage receipt binding")
        if self.status == "done":
            if (
                self.reason_code != "completed"
                or self.source_tree is None
                or SHA.fullmatch(self.source_tree) is None
                or self.file_count is None
                or self.file_count < 0
                or self.total_bytes is None
                or self.total_bytes < 0
            ):
                raise ValueError("successful GitHub stage receipt incomplete")
        elif self.source_tree is not None or self.file_count is not None or self.total_bytes is not None:
            raise ValueError("unknown GitHub stage receipt carries success evidence")
        return self


def manifest_payload(
    *,
    job_id: UUID,
    project_id: UUID,
    run_id: UUID,
    approval_id: UUID,
    actor_id: str,
    proposal_digest: str,
    source: dict[str, str],
) -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "target_id": "github_stage_offline_v1",
        "job_id": str(job_id),
        "project_id": str(project_id),
        "run_id": str(run_id),
        "approval_id": str(approval_id),
        "actor_id": actor_id,
        "proposal_digest": proposal_digest,
        "source": source,
    }
    payload = {**unsigned, "request_sha256": digest(unsigned)}
    StageManifest.model_validate(payload)
    return payload
