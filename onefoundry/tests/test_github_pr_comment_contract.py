"""Offline exact GitHub PR comment proposal, admission, and readback tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from onefoundry.github_pr_comment_contract import (
    ApprovalDecision,
    CommentAttempt,
    CommentProposal,
    CommentReceipt,
    ContractError,
    CurrentTarget,
    PostObservation,
    ReadbackObservation,
    SavedSource,
    admit_attempt,
    approval_record,
    classify_receipt,
    make_proposal,
)

PROJECT = UUID("00000000-0000-4000-8000-000000000001")
OTHER_PROJECT = UUID("00000000-0000-4000-8000-000000000002")
RUN = UUID("00000000-0000-4000-8000-000000000003")
ORDER = UUID("00000000-0000-4000-8000-000000000004")
APPROVAL = UUID("00000000-0000-4000-8000-000000000005")
SOURCE = UUID("00000000-0000-4000-8000-000000000006")
ACCOUNT = UUID("00000000-0000-4000-8000-000000000007")
KEY = UUID("00000000-0000-4000-8000-000000000008")
ATTEMPT = UUID("00000000-0000-4000-8000-000000000009")
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
REVISION = "a" * 40
HEAD = "b" * 40
COMMENT = "Reviewed exact offline fixture.\nNo external post was made."


def _proposal() -> CommentProposal:
    source = SavedSource(
        project_id=PROJECT,
        source_id=SOURCE,
        account_id=ACCOUNT,
        owner="example",
        repo="sample",
        branch="main",
        saved_revision=REVISION,
    )
    return make_proposal(
        project_id=PROJECT,
        run_id=RUN,
        work_order_id=ORDER,
        actor_id="operator",
        approval_id=APPROVAL,
        source=source,
        pr_number=17,
        head_sha=HEAD,
        comment=COMMENT,
        expires_at=NOW + timedelta(minutes=10),
        idempotency_key=KEY,
    )


def _approved(proposal: CommentProposal) -> ApprovalDecision:
    return ApprovalDecision(
        project_id=PROJECT,
        run_id=RUN,
        approval_id=APPROVAL,
        proposal_digest=proposal.proposal_digest,
        outcome="approved",
    )


def _current(proposal: CommentProposal) -> CurrentTarget:
    return CurrentTarget(
        project_id=PROJECT,
        project_status="active",
        source=proposal.source,
        pr_number=17,
        head_sha=HEAD,
    )


def _attempt() -> CommentAttempt:
    proposal = _proposal()
    return admit_attempt(
        proposal,
        _approved(proposal),
        _current(proposal),
        now=NOW,
        attempt_id=ATTEMPT,
        prior_attempt_exists=False,
    )


def _readback(attempt: CommentAttempt, **changes: object) -> ReadbackObservation:
    data: dict[str, object] = {
        "attempt_id": attempt.attempt_id,
        "readback_ordinal": 1,
        "outcome": "matched",
        "project_id": attempt.project_id,
        "owner": attempt.source.owner,
        "repo": attempt.source.repo,
        "pr_number": attempt.pr_number,
        "head_sha": attempt.head_sha,
        "comment_id": 123,
        "comment": attempt.comment,
    }
    data.update(changes)
    return ReadbackObservation.model_validate(data)


def test_exact_approved_comment_and_single_readback() -> None:
    attempt = _attempt()
    post = PostObservation(
        attempt_id=ATTEMPT, post_ordinal=1, outcome="acknowledged", http_status=201, comment_id=123
    )
    receipt = classify_receipt(attempt, post, _readback(attempt))
    assert receipt.status == "done"
    assert receipt.reason_code == "readback_matched"
    assert receipt.comment_id == 123
    assert receipt.retry_allowed is False
    assert receipt.request_sha256 == attempt.request_sha256


def test_rejects_secret_comment_and_spoofed_actor() -> None:
    proposal = _proposal()
    arguments = {
        "project_id": proposal.project_id,
        "run_id": proposal.run_id,
        "work_order_id": proposal.work_order_id,
        "actor_id": proposal.actor_id,
        "approval_id": proposal.approval_id,
        "source": proposal.source,
        "pr_number": proposal.pr_number,
        "head_sha": proposal.head_sha,
        "comment": proposal.comment,
        "expires_at": proposal.expires_at,
        "idempotency_key": proposal.idempotency_key,
    }
    with pytest.raises(ValidationError):
        make_proposal(**cast(Any, {**arguments, "comment": "password=private-value"}))
    with pytest.raises(ValidationError):
        make_proposal(**cast(Any, {**arguments, "actor_id": "operator\nspoof"}))


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": OTHER_PROJECT},
        {"run_id": OTHER_PROJECT},
        {"approval_id": OTHER_PROJECT},
        {"proposal_digest": "0" * 64},
        {"outcome": "pending"},
        {"outcome": "rejected"},
    ],
)
def test_approval_must_be_exact(change: dict[str, object]) -> None:
    proposal = _proposal()
    approval = _approved(proposal).model_copy(update=change)
    with pytest.raises(ContractError, match="comment_approval_not_bound"):
        admit_attempt(
            proposal, approval, _current(proposal), now=NOW, attempt_id=ATTEMPT, prior_attempt_exists=False
        )


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": OTHER_PROJECT},
        {"project_status": "archived"},
        {"source": None},
        {"pr_number": 18},
    ],
)
def test_source_and_head_must_be_current(change: dict[str, object]) -> None:
    proposal = _proposal()
    current = _current(proposal).model_copy(update=change)
    with pytest.raises(ContractError, match="comment_target_not_current"):
        admit_attempt(
            proposal, _approved(proposal), current, now=NOW, attempt_id=ATTEMPT, prior_attempt_exists=False
        )


def test_source_account_and_revision_drift_are_rejected() -> None:
    proposal = _proposal()
    for change in ({"account_id": UUID(int=42)}, {"saved_revision": "c" * 40}):
        drifted = proposal.source.model_copy(update=change)
        current = _current(proposal).model_copy(update={"source": drifted})
        with pytest.raises(ContractError, match="comment_target_not_current"):
            admit_attempt(
                proposal,
                _approved(proposal),
                current,
                now=NOW,
                attempt_id=ATTEMPT,
                prior_attempt_exists=False,
            )


def test_expiry_idempotency_and_clock_are_fail_closed() -> None:
    proposal = _proposal()
    approval, current = _approved(proposal), _current(proposal)
    with pytest.raises(ContractError, match="comment_approval_expired"):
        admit_attempt(
            proposal,
            approval,
            current,
            now=proposal.expires_at,
            attempt_id=ATTEMPT,
            prior_attempt_exists=False,
        )
    with pytest.raises(ContractError, match="comment_attempt_already_exists"):
        admit_attempt(proposal, approval, current, now=NOW, attempt_id=ATTEMPT, prior_attempt_exists=True)
    with pytest.raises(ContractError, match="clock_must_be_utc"):
        admit_attempt(
            proposal,
            approval,
            current,
            now=NOW.replace(tzinfo=None),
            attempt_id=ATTEMPT,
            prior_attempt_exists=False,
        )


def test_proposal_rejects_tampering_and_wrong_project() -> None:
    proposal = _proposal()
    with pytest.raises(ValidationError):
        CommentProposal.model_validate({**proposal.model_dump(), "comment": "changed"})
    with pytest.raises(ValidationError):
        make_proposal(
            project_id=OTHER_PROJECT,
            run_id=RUN,
            work_order_id=ORDER,
            actor_id="operator",
            approval_id=APPROVAL,
            source=proposal.source,
            pr_number=17,
            head_sha=HEAD,
            comment=COMMENT,
            expires_at=NOW,
            idempotency_key=KEY,
        )
    with pytest.raises(ValidationError):
        CommentProposal.model_validate({**proposal.model_dump(), "extra": "denied"})
    with pytest.raises(ValidationError):
        make_proposal(
            project_id=PROJECT,
            run_id=RUN,
            work_order_id=ORDER,
            actor_id="operator",
            approval_id=APPROVAL,
            source=proposal.source,
            pr_number=0,
            head_sha=HEAD,
            comment=COMMENT,
            expires_at=NOW,
            idempotency_key=KEY,
        )
    with pytest.raises(ValidationError):
        make_proposal(
            project_id=PROJECT,
            run_id=RUN,
            work_order_id=ORDER,
            actor_id="operator",
            approval_id=APPROVAL,
            source=proposal.source,
            pr_number=17,
            head_sha=HEAD,
            comment="é" * 9_000,
            expires_at=NOW,
            idempotency_key=KEY,
        )


def test_proposal_digest_matches_existing_approval_shape() -> None:
    import hashlib
    import json

    proposal = _proposal()
    target, content, digest = approval_record(proposal)
    wrapped = {"action_type": "other", "target": target, "content": content}
    expected = hashlib.sha256(
        json.dumps(wrapped, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert digest == expected == proposal.proposal_digest
    assert json.loads(content)["comment_sha256"] == proposal.comment_sha256


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": OTHER_PROJECT},
        {"owner": "other"},
        {"repo": "other"},
        {"pr_number": 18},
        {"comment_id": 124},
        {"comment": "different"},
    ],
)
def test_readback_mismatch_is_unknown(change: dict[str, object]) -> None:
    attempt = _attempt()
    post = PostObservation(
        attempt_id=ATTEMPT, post_ordinal=1, outcome="acknowledged", http_status=201, comment_id=123
    )
    receipt = classify_receipt(attempt, post, _readback(attempt, **change))
    assert receipt.status == "unknown"
    assert receipt.reason_code == "readback_unverified"
    assert receipt.retry_allowed is False


def test_comment_receipt_remains_done_when_pr_head_moves_after_post() -> None:
    attempt = _attempt()
    post = PostObservation(
        attempt_id=ATTEMPT, post_ordinal=1, outcome="acknowledged", http_status=201, comment_id=123
    )
    receipt = classify_receipt(attempt, post, _readback(attempt, head_sha="c" * 40))
    assert receipt.status == "done"
    assert receipt.reason_code == "readback_matched"


def test_approval_target_warns_that_pr_commits_can_change() -> None:
    target, _, _ = approval_record(_proposal())
    assert "PR #17 in example/sample" in target
    assert "commits may change before posting" in target


@pytest.mark.parametrize(
    "post,readback,status,reason",
    [
        ("not_sent", None, "failed", "request_not_sent"),
        ("ambiguous", None, "unknown", "post_ambiguous"),
        ("acknowledged", None, "unknown", "readback_unverified"),
    ],
)
def test_no_readback_or_ambiguous_post_never_claims_success(
    post: str, readback: None, status: str, reason: str
) -> None:
    attempt = _attempt()
    observation = PostObservation.model_validate(
        {
            "attempt_id": ATTEMPT,
            "post_ordinal": 1,
            "outcome": post,
            "http_status": 201 if post == "acknowledged" else None,
            "comment_id": 123 if post == "acknowledged" else None,
        }
    )
    receipt = classify_receipt(attempt, observation, readback)
    assert (receipt.status, receipt.reason_code, receipt.comment_id, receipt.retry_allowed) == (
        status,
        reason,
        None,
        False,
    )


def test_ambiguous_post_stays_unknown_even_with_matching_readback() -> None:
    attempt = _attempt()
    receipt = classify_receipt(
        attempt,
        PostObservation(attempt_id=ATTEMPT, post_ordinal=1, outcome="ambiguous"),
        _readback(attempt),
    )
    assert receipt.status == "unknown"
    assert receipt.reason_code == "post_ambiguous"


@pytest.mark.parametrize("outcome", ["missing", "mismatch", "unavailable"])
def test_unverified_readback_is_unknown(outcome: str) -> None:
    attempt = _attempt()
    post = PostObservation(
        attempt_id=ATTEMPT, post_ordinal=1, outcome="acknowledged", http_status=201, comment_id=123
    )
    readback = ReadbackObservation.model_validate(
        {
            "attempt_id": ATTEMPT,
            "readback_ordinal": 1,
            "outcome": outcome,
        }
    )
    receipt = classify_receipt(attempt, post, readback)
    assert receipt.status == "unknown"
    assert receipt.retry_allowed is False


def test_observations_must_match_exact_attempt_and_shape() -> None:
    attempt = _attempt()
    with pytest.raises(ContractError, match="comment_observation_not_bound"):
        classify_receipt(
            attempt, PostObservation(attempt_id=UUID(int=42), post_ordinal=1, outcome="ambiguous"), None
        )
    with pytest.raises(ValidationError):
        PostObservation(attempt_id=ATTEMPT, post_ordinal=1, outcome="acknowledged")
    with pytest.raises(ValidationError):
        PostObservation(
            attempt_id=ATTEMPT, post_ordinal=1, outcome="acknowledged", http_status=200, comment_id=123
        )
    with pytest.raises(ValidationError):
        PostObservation.model_validate({"attempt_id": ATTEMPT, "post_ordinal": 2, "outcome": "ambiguous"})
    with pytest.raises(ValidationError):
        ReadbackObservation(attempt_id=ATTEMPT, readback_ordinal=1, outcome="missing", comment_id=123)
    with pytest.raises(ValidationError):
        ReadbackObservation.model_validate(
            {"attempt_id": ATTEMPT, "readback_ordinal": 2, "outcome": "unavailable"}
        )
    with pytest.raises(ValidationError):
        CommentReceipt(
            attempt_id=ATTEMPT,
            project_id=PROJECT,
            run_id=RUN,
            approval_id=APPROVAL,
            request_sha256="a" * 64,
            status="done",
            reason_code="post_ambiguous",
            comment_id=None,
            retry_allowed=False,
        )
