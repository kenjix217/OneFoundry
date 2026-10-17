"""Gateway-owned, one-shot execution and readback of approved PR comments."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.github_pr_comment_contract import (
    ApprovalDecision,
    CommentAttempt,
    CommentProposal,
    CurrentTarget,
    PostObservation,
    ReadbackObservation,
    admit_attempt,
    approval_record,
    classify_receipt,
)
from onefoundry.github_pr_comment_proposal import _source
from onefoundry.github_pr_comment_transport import GitHubCommentTransport
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.role_tool_admission import ToolDenied, _context
from onefoundry.source_github import GitHubUnavailable

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class CommentTransport(Protocol):
    def current_head(self, credential: str, source: Any, number: int) -> str: ...
    def post_comment(self, credential: str, source: Any, number: int, body: str) -> int: ...
    def read_comment(self, credential: str, source: Any, number: int, comment_id: int) -> str: ...


def _approval(connection: Connection, project_id: UUID, approval_id: UUID, *, lock: bool) -> Any:
    statement = (
        "SELECT id,project_id,run_id,work_order_id,actor_id,status,action_type,target,content,"
        "proposal_digest FROM approval_requests WHERE project_id=:project_id AND id=:id"
    )
    if lock:
        statement += " FOR UPDATE"
    row = (
        connection.execute(text(statement), {"project_id": project_id, "id": approval_id})
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("PR comment proposal not found")
    return row


def _proposal(connection: Connection, approval: Any) -> CommentProposal:
    marker = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:project_id AND run_id=:run_id "
            "AND event_type='github_pr_comment_proposed' "
            "AND payload->>'approval_id'=:approval_id "
            "AND payload->>'proposal_digest'=:digest LIMIT 1"
        ),
        {
            "project_id": approval["project_id"],
            "run_id": approval["run_id"],
            "approval_id": str(approval["id"]),
            "digest": approval["proposal_digest"],
        },
    )
    if marker is None:
        raise NotFound("PR comment proposal not found")
    try:
        proposal = CommentProposal.model_validate_json(
            json.dumps({**json.loads(approval["content"]), "proposal_digest": approval["proposal_digest"]})
        )
        target, content, digest = approval_record(proposal)
    except (TypeError, ValueError, ValidationError):
        raise Conflict("PR comment proposal is invalid") from None
    if (
        approval["action_type"] != "other"
        or approval["target"] != target
        or approval["content"] != content
        or approval["proposal_digest"] != digest
        or approval["project_id"] != proposal.project_id
        or approval["id"] != proposal.approval_id
        or approval["run_id"] != proposal.run_id
        or approval["work_order_id"] != proposal.work_order_id
        or approval["actor_id"] != proposal.actor_id
    ):
        raise Conflict("PR comment approval binding changed")
    return proposal


def _events(connection: Connection, project_id: UUID, approval_id: UUID) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT event_type,payload FROM events WHERE project_id=:project_id "
                "AND payload->>'approval_id'=:approval_id "
                "AND event_type IN ('github_pr_comment_attempted',"
                "'github_pr_comment_post_observed','github_pr_comment_readback_started',"
                "'github_pr_comment_receipt') ORDER BY created_at,id"
            ),
            {"project_id": project_id, "approval_id": str(approval_id)},
        ).mappings()
    ]


def _event(
    gateway: ToolGateway,
    connection: Connection,
    proposal: CommentProposal,
    kind: str,
    payload: dict[str, Any],
) -> None:
    gateway._event(
        connection,
        proposal.project_id,
        proposal.work_order_id,
        proposal.run_id,
        kind,
        {"approval_id": str(proposal.approval_id), **payload},
    )


def _credential(gateway: ToolGateway, account_id: UUID) -> str:
    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("PR comment settings unavailable")
    try:
        return ModelSecretStore(str(Path(settings.model_secret_root) / "github")).read(account_id)
    except SecretStoreError:
        raise Unavailable("saved GitHub credential unavailable") from None


def _transport(gateway: ToolGateway, given: CommentTransport | None) -> CommentTransport:
    if given is not None:
        return given
    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("PR comment settings unavailable")
    return GitHubCommentTransport(settings)


def _active_role(gateway: ToolGateway, connection: Connection, proposal: CommentProposal) -> None:
    profile_id = connection.scalar(
        text("SELECT profile_id FROM runs WHERE project_id=:project_id AND id=:run_id"),
        {"project_id": proposal.project_id, "run_id": proposal.run_id},
    )
    if profile_id is None:
        raise Conflict("PR comment run is no longer active")
    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("PR comment settings unavailable")
    try:
        context = _context(
            connection,
            proposal.project_id,
            proposal.run_id,
            profile_id,
            lock=True,
            max_items=settings.role_context_max_items,
        )
    except (ToolDenied, NotFound):
        raise Conflict("PR comment run is no longer active") from None
    if (
        context.work_order_id != proposal.work_order_id
        or "github_pr_comment_v1" not in context.snapshot_tools & context.current_tools
    ):
        raise Conflict("PR comment role permission changed")


def _record_receipt(
    gateway: ToolGateway,
    proposal: CommentProposal,
    attempt: CommentAttempt,
    post: PostObservation,
    readback: ReadbackObservation | None,
) -> dict[str, Any]:
    receipt = classify_receipt(attempt, post, readback)
    with gateway.engine.begin() as connection:
        _approval(connection, proposal.project_id, proposal.approval_id, lock=True)
        existing = _events(connection, proposal.project_id, proposal.approval_id)
        terminal = next(
            (item for item in existing if item["event_type"] == "github_pr_comment_receipt"), None
        )
        if terminal is not None:
            return dict(terminal["payload"]["receipt"])
        _event(
            gateway,
            connection,
            proposal,
            "github_pr_comment_receipt",
            {"attempt_id": str(attempt.attempt_id), "receipt": receipt.model_dump(mode="json")},
        )
    return receipt.model_dump(mode="json")


def status(gateway: ToolGateway, project_id: UUID, approval_id: UUID) -> dict[str, Any]:
    with gateway.engine.connect() as connection:
        approval = _approval(connection, project_id, approval_id, lock=False)
        _proposal(connection, approval)
        events = _events(connection, project_id, approval_id)
    terminal = next((item for item in events if item["event_type"] == "github_pr_comment_receipt"), None)
    if terminal is not None:
        return dict(terminal["payload"]["receipt"])
    attempt = next((item for item in events if item["event_type"] == "github_pr_comment_attempted"), None)
    if attempt is not None:
        post = next(
            (item for item in events if item["event_type"] == "github_pr_comment_post_observed"), None
        )
        readback_started = any(item["event_type"] == "github_pr_comment_readback_started" for item in events)
        reason = "attempt_unreconciled"
        state = "unknown"
        if post is not None:
            observed = PostObservation.model_validate_json(json.dumps(post["payload"]["post"]))
            if observed.outcome == "acknowledged":
                reason = "readback_unknown" if readback_started else "readback_pending"
            elif observed.outcome == "not_sent":
                reason = "request_not_sent"
                state = "failed"
            else:
                reason = "post_ambiguous"
        return {
            "approval_id": str(approval_id),
            "attempt_id": attempt["payload"]["attempt_id"],
            "status": state,
            "reason_code": reason,
            "retry_allowed": False,
            "comment_id": None,
        }
    return {"approval_id": str(approval_id), "status": "not_attempted", "retry_allowed": False}


def dispatch(
    gateway: ToolGateway, project_id: UUID, approval_id: UUID, *, transport: CommentTransport | None = None
) -> dict[str, Any]:
    """Persist an exact attempt before any POST; never repeat it after uncertainty."""
    outbound = _transport(gateway, transport)
    with gateway.engine.connect() as connection:
        approval = _approval(connection, project_id, approval_id, lock=False)
        proposal = _proposal(connection, approval)
        if approval["status"] != "approved":
            raise Conflict("PR comment proposal must be approved")
        _active_role(gateway, connection, proposal)
        if _events(connection, project_id, approval_id):
            raise Conflict("PR comment already attempted")
    credential = _credential(gateway, proposal.source.account_id)
    try:
        head = outbound.current_head(credential, proposal.source, proposal.pr_number)
    except (GitHubUnavailable, OSError, ValueError):
        raise Unavailable("GitHub PR head unavailable") from None
    if head != proposal.head_sha:
        raise Conflict("GitHub PR head changed; comment not sent")
    with gateway.engine.begin() as connection:
        approval = _approval(connection, project_id, approval_id, lock=True)
        proposal = _proposal(connection, approval)
        if approval["status"] != "approved":
            raise Conflict("PR comment proposal must be approved")
        _active_role(gateway, connection, proposal)
        if _events(connection, project_id, approval_id):
            raise Conflict("PR comment already attempted")
        source = _source(connection, project_id, proposal.source.source_id)
        active = connection.scalar(
            text("SELECT status FROM projects WHERE id=:id FOR SHARE"), {"id": project_id}
        )
        current = CurrentTarget(
            project_id=project_id,
            project_status=active,
            source=source,
            pr_number=proposal.pr_number,
            head_sha=head,
        )
        attempt = admit_attempt(
            proposal,
            ApprovalDecision(
                project_id=project_id,
                run_id=proposal.run_id,
                approval_id=approval_id,
                proposal_digest=approval["proposal_digest"],
                outcome=approval["status"],
            ),
            current,
            now=datetime.now(UTC),
            attempt_id=uuid4(),
            prior_attempt_exists=False,
        )
        _event(
            gateway,
            connection,
            proposal,
            "github_pr_comment_attempted",
            {
                "attempt_id": str(attempt.attempt_id),
                "request_sha256": attempt.request_sha256,
                "proposal_digest": proposal.proposal_digest,
                "head_sha": proposal.head_sha,
                "comment_sha256": proposal.comment_sha256,
            },
        )
    with gateway.engine.begin() as connection:
        # This transaction protects the project and saved source until the
        # outbound request has returned. The prior attempt event is already
        # committed, so a crash or failed commit cannot permit a second POST.
        settings = gateway.live_text_settings
        if settings is None:
            raise Unavailable("PR comment settings unavailable")
        connection.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": f"{settings.github_timeout_seconds * 1000}ms"},
        )
        current_approval = _approval(connection, project_id, approval_id, lock=True)
        try:
            _active_role(gateway, connection, proposal)
            role_active = True
        except Conflict:
            role_active = False
        active = connection.scalar(
            text("SELECT status FROM projects WHERE id=:id FOR SHARE"), {"id": project_id}
        )
        source = _source(connection, project_id, proposal.source.source_id)
        if (
            not role_active
            or current_approval["status"] != "approved"
            or active != "active"
            or source != proposal.source
        ):
            post = PostObservation(attempt_id=attempt.attempt_id, post_ordinal=1, outcome="not_sent")
        else:
            try:
                head_again = outbound.current_head(credential, proposal.source, proposal.pr_number)
            except (GitHubUnavailable, OSError, ValueError):
                head_again = None
            if head_again != proposal.head_sha:
                post = PostObservation(attempt_id=attempt.attempt_id, post_ordinal=1, outcome="not_sent")
            else:
                try:
                    comment_id = outbound.post_comment(
                        credential, proposal.source, proposal.pr_number, proposal.comment
                    )
                    post = PostObservation(
                        attempt_id=attempt.attempt_id,
                        post_ordinal=1,
                        outcome="acknowledged",
                        http_status=201,
                        comment_id=comment_id,
                    )
                except (GitHubUnavailable, OSError, ValueError, ValidationError):
                    post = PostObservation(attempt_id=attempt.attempt_id, post_ordinal=1, outcome="ambiguous")
        _event(
            gateway,
            connection,
            proposal,
            "github_pr_comment_post_observed",
            {"attempt_id": str(attempt.attempt_id), "post": post.model_dump(mode="json")},
        )
    if post.outcome != "acknowledged":
        return _record_receipt(gateway, proposal, attempt, post, None)
    return reconcile(gateway, project_id, approval_id, transport=outbound)


def reconcile(
    gateway: ToolGateway, project_id: UUID, approval_id: UUID, *, transport: CommentTransport | None = None
) -> dict[str, Any]:
    """Read an acknowledged comment once; never issue another POST."""
    outbound = _transport(gateway, transport)
    with gateway.engine.begin() as connection:
        approval = _approval(connection, project_id, approval_id, lock=True)
        proposal = _proposal(connection, approval)
        events = _events(connection, project_id, approval_id)
        terminal = next((item for item in events if item["event_type"] == "github_pr_comment_receipt"), None)
        if terminal is not None:
            return dict(terminal["payload"]["receipt"])
        post_event = next(
            (item for item in events if item["event_type"] == "github_pr_comment_post_observed"), None
        )
        attempted = next(
            (item for item in events if item["event_type"] == "github_pr_comment_attempted"), None
        )
        if post_event is None or attempted is None:
            raise Conflict("PR comment has no acknowledged POST to reconcile")
        post = PostObservation.model_validate_json(json.dumps(post_event["payload"]["post"]))
        if post.outcome != "acknowledged":
            raise Conflict("PR comment POST was not acknowledged")
        if post.comment_id is None:
            raise Conflict("PR comment acknowledgment lacks an ID")
        if any(item["event_type"] == "github_pr_comment_readback_started" for item in events):
            raise Conflict("PR comment readback already attempted")
        attempt = CommentAttempt.model_validate(
            {
                "attempt_id": UUID(attempted["payload"]["attempt_id"]),
                "project_id": project_id,
                "run_id": proposal.run_id,
                "approval_id": approval_id,
                "proposal_digest": proposal.proposal_digest,
                "source": proposal.source,
                "pr_number": proposal.pr_number,
                "head_sha": proposal.head_sha,
                "comment": proposal.comment,
                "comment_sha256": proposal.comment_sha256,
                "idempotency_key": proposal.idempotency_key,
                "request_sha256": attempted["payload"]["request_sha256"],
            }
        )
        _event(
            gateway,
            connection,
            proposal,
            "github_pr_comment_readback_started",
            {
                "attempt_id": str(attempt.attempt_id),
                "comment_id": post.comment_id,
            },
        )
    credential = _credential(gateway, proposal.source.account_id)
    readback: ReadbackObservation
    try:
        body = outbound.read_comment(credential, proposal.source, proposal.pr_number, post.comment_id)
        head = outbound.current_head(credential, proposal.source, proposal.pr_number)
        readback = ReadbackObservation(
            attempt_id=attempt.attempt_id,
            readback_ordinal=1,
            outcome="matched",
            project_id=project_id,
            owner=proposal.source.owner,
            repo=proposal.source.repo,
            pr_number=proposal.pr_number,
            head_sha=head,
            comment_id=post.comment_id,
            comment=body,
        )
    except (GitHubUnavailable, OSError, ValueError, ValidationError):
        readback = ReadbackObservation(
            attempt_id=attempt.attempt_id, readback_ordinal=1, outcome="unavailable"
        )
    return _record_receipt(gateway, proposal, attempt, post, readback)
