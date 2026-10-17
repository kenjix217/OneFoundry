"""Contract for one approved GitHub PR comment attempt and readback.

No HTTP, database, credential, or retry operation lives here. The future
dispatcher must lock the project/run/source/approval, persist an attempt with a
unique idempotency key before POST, and record the single readback observation.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.source_github import BRANCH, REPO_PART, SHA

SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_COMMENT_CHARS = 16_000
MAX_COMMENT_BYTES = 16_000


class ContractError(ValueError):
    """An exact owner or approval binding did not hold."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(None)


def _comment_hash(body: str) -> str:
    try:
        encoded = body.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("invalid comment encoding") from None
    return hashlib.sha256(encoded).hexdigest()


def _approval_target(source: SavedSource, pr_number: int, head_sha: str) -> str:
    # The observed head is retained in the proposal for a pre-POST safety check.
    # Issue comments attach to the PR as a whole; they cannot bind to a commit.
    return (
        f"Comment on GitHub PR #{pr_number} in {source.owner}/{source.repo} "
        "(commits may change before posting)"
    )


def _approval_digest(unsigned: dict[str, Any], source: SavedSource, pr_number: int, head_sha: str) -> str:
    content = _canonical(unsigned).decode("utf-8")
    return _digest(
        {"action_type": "other", "target": _approval_target(source, pr_number, head_sha), "content": content}
    )


class SavedSource(StrictModel):
    project_id: UUID
    source_id: UUID
    account_id: UUID
    owner: str
    repo: str
    branch: str
    saved_revision: str

    @model_validator(mode="after")
    def valid_source(self) -> SavedSource:
        if (
            REPO_PART.fullmatch(self.owner) is None
            or REPO_PART.fullmatch(self.repo) is None
            or BRANCH.fullmatch(self.branch) is None
            or SHA.fullmatch(self.saved_revision) is None
        ):
            raise ValueError("invalid saved GitHub source")
        return self


class CommentProposal(StrictModel):
    schema_version: Literal[1]
    action: Literal["github_pr_comment_v1"]
    project_id: UUID
    run_id: UUID
    work_order_id: UUID
    actor_id: str = Field(min_length=1, max_length=128)
    approval_id: UUID
    source: SavedSource
    pr_number: int = Field(ge=1, le=999_999_999)
    head_sha: str
    comment: str = Field(min_length=1, max_length=MAX_COMMENT_CHARS)
    comment_sha256: str
    expires_at: datetime
    idempotency_key: UUID
    proposal_digest: str

    @model_validator(mode="after")
    def exact_proposal(self) -> CommentProposal:
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_./-]{0,127}", self.actor_id) is None
            or contains_sensitive_text(self.comment)
            or self.source.project_id != self.project_id
            or SHA.fullmatch(self.head_sha) is None
            or SHA256.fullmatch(self.comment_sha256) is None
            or self.comment_sha256 != _comment_hash(self.comment)
            or len(self.comment.encode("utf-8")) > MAX_COMMENT_BYTES
            or not _utc(self.expires_at)
            or "\x00" in self.comment
            or "\r" in self.comment
            or SHA256.fullmatch(self.proposal_digest) is None
            or self.proposal_digest
            != _approval_digest(
                self.model_dump(mode="json", exclude={"proposal_digest"}),
                self.source,
                self.pr_number,
                self.head_sha,
            )
        ):
            raise ValueError("invalid exact PR comment proposal")
        return self


def make_proposal(
    *,
    project_id: UUID,
    run_id: UUID,
    work_order_id: UUID,
    actor_id: str,
    approval_id: UUID,
    source: SavedSource,
    pr_number: int,
    head_sha: str,
    comment: str,
    expires_at: datetime,
    idempotency_key: UUID,
) -> CommentProposal:
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "action": "github_pr_comment_v1",
        "project_id": project_id,
        "run_id": run_id,
        "work_order_id": work_order_id,
        "actor_id": actor_id,
        "approval_id": approval_id,
        "source": source,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "comment": comment,
        "comment_sha256": _comment_hash(comment),
        "expires_at": expires_at,
        "idempotency_key": idempotency_key,
    }
    normalized = CommentProposal.model_construct(**unsigned).model_dump(mode="json")
    return CommentProposal.model_validate(
        {
            **unsigned,
            "proposal_digest": _approval_digest(normalized, source, pr_number, head_sha),
        }
    )


def approval_record(proposal: CommentProposal) -> tuple[str, str, str]:
    """Return existing approval_requests target/content/digest semantics."""
    unsigned = proposal.model_dump(mode="json", exclude={"proposal_digest"})
    return (
        _approval_target(proposal.source, proposal.pr_number, proposal.head_sha),
        _canonical(unsigned).decode("utf-8"),
        proposal.proposal_digest,
    )


class ApprovalDecision(StrictModel):
    project_id: UUID
    run_id: UUID
    approval_id: UUID
    proposal_digest: str
    outcome: Literal["pending", "approved", "rejected"]

    @model_validator(mode="after")
    def valid_digest(self) -> ApprovalDecision:
        if SHA256.fullmatch(self.proposal_digest) is None:
            raise ValueError("invalid approval digest")
        return self


class CurrentTarget(StrictModel):
    project_id: UUID
    project_status: Literal["active", "archived"]
    source: SavedSource | None
    pr_number: int = Field(ge=1, le=999_999_999)
    head_sha: str

    @model_validator(mode="after")
    def valid_head(self) -> CurrentTarget:
        if SHA.fullmatch(self.head_sha) is None:
            raise ValueError("invalid current PR head")
        return self


class CommentAttempt(StrictModel):
    attempt_id: UUID
    project_id: UUID
    run_id: UUID
    approval_id: UUID
    proposal_digest: str
    source: SavedSource
    pr_number: int = Field(ge=1, le=999_999_999)
    head_sha: str
    comment: str = Field(min_length=1, max_length=MAX_COMMENT_CHARS)
    comment_sha256: str
    idempotency_key: UUID
    request_sha256: str

    @model_validator(mode="after")
    def exact_attempt(self) -> CommentAttempt:
        if (
            self.source.project_id != self.project_id
            or SHA.fullmatch(self.head_sha) is None
            or self.comment_sha256 != _comment_hash(self.comment)
            or len(self.comment.encode("utf-8")) > MAX_COMMENT_BYTES
            or SHA256.fullmatch(self.proposal_digest) is None
            or SHA256.fullmatch(self.request_sha256) is None
            or self.request_sha256 != _digest(self.model_dump(mode="json", exclude={"request_sha256"}))
        ):
            raise ValueError("invalid exact PR comment attempt")
        return self


def admit_attempt(
    proposal: CommentProposal,
    approval: ApprovalDecision,
    current: CurrentTarget,
    *,
    now: datetime,
    attempt_id: UUID,
    prior_attempt_exists: bool,
) -> CommentAttempt:
    """Check exact owner state before the caller records its unique attempt."""
    if not _utc(now):
        raise ContractError("clock_must_be_utc")
    if prior_attempt_exists:
        raise ContractError("comment_attempt_already_exists")
    if (
        approval.project_id != proposal.project_id
        or approval.run_id != proposal.run_id
        or approval.approval_id != proposal.approval_id
        or approval.proposal_digest != proposal.proposal_digest
        or approval.outcome != "approved"
    ):
        raise ContractError("comment_approval_not_bound")
    if now >= proposal.expires_at:
        raise ContractError("comment_approval_expired")
    if (
        current.project_id != proposal.project_id
        or current.project_status != "active"
        or current.source != proposal.source
        or current.pr_number != proposal.pr_number
        or current.head_sha != proposal.head_sha
    ):
        raise ContractError("comment_target_not_current")
    unsigned: dict[str, Any] = {
        "attempt_id": attempt_id,
        "project_id": proposal.project_id,
        "run_id": proposal.run_id,
        "approval_id": proposal.approval_id,
        "proposal_digest": proposal.proposal_digest,
        "source": proposal.source,
        "pr_number": proposal.pr_number,
        "head_sha": proposal.head_sha,
        "comment": proposal.comment,
        "comment_sha256": proposal.comment_sha256,
        "idempotency_key": proposal.idempotency_key,
    }
    normalized = CommentAttempt.model_construct(**unsigned).model_dump(mode="json")
    return CommentAttempt.model_validate({**unsigned, "request_sha256": _digest(normalized)})


class PostObservation(StrictModel):
    attempt_id: UUID
    post_ordinal: Literal[1]
    outcome: Literal["not_sent", "acknowledged", "ambiguous"]
    http_status: int | None = None
    comment_id: int | None = None

    @model_validator(mode="after")
    def exact_post(self) -> PostObservation:
        if self.outcome == "acknowledged":
            if self.comment_id is None or self.http_status != 201:
                raise ValueError("post acknowledgment requires HTTP 201 and one comment ID")
        elif self.comment_id is not None or self.http_status is not None:
            raise ValueError("unconfirmed post carries success evidence")
        if self.comment_id is not None and self.comment_id <= 0:
            raise ValueError("invalid comment ID")
        return self


class ReadbackObservation(StrictModel):
    attempt_id: UUID
    readback_ordinal: Literal[1]
    outcome: Literal["matched", "missing", "mismatch", "unavailable"]
    project_id: UUID | None = None
    owner: str | None = None
    repo: str | None = None
    pr_number: int | None = None
    head_sha: str | None = None
    comment_id: int | None = None
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)

    @model_validator(mode="after")
    def exact_readback(self) -> ReadbackObservation:
        evidence = (
            self.project_id,
            self.owner,
            self.repo,
            self.pr_number,
            self.head_sha,
            self.comment_id,
            self.comment,
        )
        if self.outcome == "matched":
            if (
                any(value is None for value in evidence)
                or not isinstance(self.owner, str)
                or REPO_PART.fullmatch(self.owner) is None
                or not isinstance(self.repo, str)
                or REPO_PART.fullmatch(self.repo) is None
                or not isinstance(self.head_sha, str)
                or SHA.fullmatch(self.head_sha) is None
                or not isinstance(self.comment_id, int)
                or self.comment_id <= 0
                or not isinstance(self.pr_number, int)
                or self.pr_number <= 0
                or not isinstance(self.comment, str)
                or len(self.comment.encode("utf-8")) > MAX_COMMENT_BYTES
            ):
                raise ValueError("readback evidence incomplete")
        elif any(value is not None for value in evidence):
            raise ValueError("nonmatching readback carries success evidence")
        return self


class CommentReceipt(StrictModel):
    attempt_id: UUID
    project_id: UUID
    run_id: UUID
    approval_id: UUID
    request_sha256: str
    status: Literal["done", "failed", "unknown"]
    reason_code: Literal["readback_matched", "request_not_sent", "post_ambiguous", "readback_unverified"]
    comment_id: int | None
    retry_allowed: Literal[False]

    @model_validator(mode="after")
    def exact_outcome(self) -> CommentReceipt:
        if SHA256.fullmatch(self.request_sha256) is None:
            raise ValueError("invalid comment receipt request hash")
        if self.status == "done":
            if self.reason_code != "readback_matched" or self.comment_id is None or self.comment_id <= 0:
                raise ValueError("successful comment receipt lacks readback")
        elif (
            self.comment_id is not None
            or (self.status == "failed" and self.reason_code != "request_not_sent")
            or (
                self.status == "unknown" and self.reason_code not in ("post_ambiguous", "readback_unverified")
            )
        ):
            raise ValueError("comment receipt status is inconsistent")
        return self


def classify_receipt(
    attempt: CommentAttempt,
    post: PostObservation,
    readback: ReadbackObservation | None,
) -> CommentReceipt:
    """Classify one POST and at most one readback; ambiguous POST never retries."""
    if post.attempt_id != attempt.attempt_id or (
        readback is not None and readback.attempt_id != attempt.attempt_id
    ):
        raise ContractError("comment_observation_not_bound")
    status: Literal["done", "failed", "unknown"] = "unknown"
    reason: Literal["readback_matched", "request_not_sent", "post_ambiguous", "readback_unverified"] = (
        "readback_unverified"
    )
    comment_id: int | None = None
    if post.outcome == "not_sent":
        status, reason = "failed", "request_not_sent"
    elif post.outcome == "ambiguous":
        reason = "post_ambiguous"
    elif (
        readback is not None
        and readback.outcome == "matched"
        and readback.project_id == attempt.project_id
        and readback.owner == attempt.source.owner
        and readback.repo == attempt.source.repo
        and readback.pr_number == attempt.pr_number
        and readback.comment_id == post.comment_id
        and readback.comment is not None
        and _comment_hash(readback.comment) == attempt.comment_sha256
    ):
        status, reason, comment_id = "done", "readback_matched", post.comment_id
    return CommentReceipt(
        attempt_id=attempt.attempt_id,
        project_id=attempt.project_id,
        run_id=attempt.run_id,
        approval_id=attempt.approval_id,
        request_sha256=attempt.request_sha256,
        status=status,
        reason_code=reason,
        comment_id=comment_id,
        retry_allowed=False,
    )
