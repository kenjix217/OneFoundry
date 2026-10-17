"""Versioned, secret-free contract for local HTTPS source verification."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from onefoundry.github_stage_contract import SHA256, digest
from onefoundry.source_github import BRANCH, REPO_PART, SHA


class FetchSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["github_fixture_fetch_v2"]
    project_id: str
    source_id: str
    account_id: str
    owner: str
    repo: str
    branch: str
    saved_revision: str

    @model_validator(mode="after")
    def exact(self) -> "FetchSource":
        if (
            any(str(UUID(value)) != value for value in (self.project_id, self.source_id, self.account_id))
            or REPO_PART.fullmatch(self.owner) is None
            or REPO_PART.fullmatch(self.repo) is None
            or BRANCH.fullmatch(self.branch) is None
            or SHA.fullmatch(self.saved_revision) is None
        ):
            raise ValueError("invalid fixture source")
        return self


def expiry(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError("fetch expiry must be UTC")
    return parsed


class FetchIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["github_fixture_fetch_v2"]
    action: Literal["verify_local_https_fixture"]
    project_id: str
    source_id: str
    account_id: str
    owner: str
    repo: str
    branch: str
    saved_revision: str
    expires_at: str
    idempotency_key: str

    @model_validator(mode="after")
    def exact(self) -> "FetchIntent":
        FetchSource.model_validate({key: getattr(self, key) for key in FetchSource.model_fields})
        if str(UUID(self.idempotency_key)) != self.idempotency_key:
            raise ValueError("invalid fetch idempotency key")
        expiry(self.expires_at)
        return self


class FetchManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2]
    target_id: Literal["github_fixture_https_v2"]
    action: Literal["verify_local_https_fixture"]
    job_id: str
    project_id: str
    run_id: str
    approval_id: str
    actor_id: str = Field(min_length=1, max_length=128)
    proposal_digest: str
    source: FetchSource
    expires_at: str
    idempotency_key: str
    request_sha256: str

    @model_validator(mode="after")
    def exact(self) -> "FetchManifest":
        if (
            any(
                str(UUID(value)) != value
                for value in (
                    self.job_id,
                    self.project_id,
                    self.run_id,
                    self.approval_id,
                    self.idempotency_key,
                )
            )
            or self.source.project_id != self.project_id
            or SHA256.fullmatch(self.proposal_digest) is None
            or self.request_sha256 != digest(self.model_dump(exclude={"request_sha256"}))
        ):
            raise ValueError("invalid fixture fetch manifest")
        expiry(self.expires_at)
        return self


class FetchReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2]
    target_id: Literal["github_fixture_https_v2"]
    action: Literal["verify_local_https_fixture"]
    job_id: str
    project_id: str
    run_id: str
    approval_id: str
    proposal_digest: str
    source: FetchSource
    expires_at: str
    idempotency_key: str
    request_sha256: str
    status: Literal["done", "unknown"]
    reason_code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    source_revision: str | None
    source_tree: str | None
    file_count: int | None
    total_bytes: int | None
    started_at: str
    finished_at: str

    @model_validator(mode="after")
    def exact(self) -> "FetchReceipt":
        if (
            any(
                str(UUID(value)) != value
                for value in (
                    self.job_id,
                    self.project_id,
                    self.run_id,
                    self.approval_id,
                    self.idempotency_key,
                )
            )
            or self.source.project_id != self.project_id
            or SHA256.fullmatch(self.proposal_digest) is None
            or SHA256.fullmatch(self.request_sha256) is None
        ):
            raise ValueError("invalid fixture receipt binding")
        expiry(self.expires_at)
        if self.status == "done":
            if (
                self.reason_code != "verified_only"
                or self.source_revision != self.source.saved_revision
                or self.source_tree is None
                or SHA.fullmatch(self.source_tree) is None
                or self.file_count is None
                or self.file_count < 0
                or self.total_bytes is None
                or self.total_bytes < 0
            ):
                raise ValueError("incomplete fixture verification receipt")
        elif any(
            value is not None
            for value in (self.source_revision, self.source_tree, self.file_count, self.total_bytes)
        ):
            raise ValueError("unknown fixture receipt contains success evidence")
        return self


def manifest_payload(
    *,
    job_id: UUID,
    project_id: UUID,
    run_id: UUID,
    approval_id: UUID,
    actor_id: str,
    proposal_digest: str,
    intent: FetchIntent,
) -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "schema_version": 2,
        "target_id": "github_fixture_https_v2",
        "action": intent.action,
        "job_id": str(job_id),
        "project_id": str(project_id),
        "run_id": str(run_id),
        "approval_id": str(approval_id),
        "actor_id": actor_id,
        "proposal_digest": proposal_digest,
        "source": intent.model_dump(include=set(FetchSource.model_fields)),
        "expires_at": intent.expires_at,
        "idempotency_key": intent.idempotency_key,
    }
    payload = {**unsigned, "request_sha256": digest(unsigned)}
    FetchManifest.model_validate(payload)
    return payload
