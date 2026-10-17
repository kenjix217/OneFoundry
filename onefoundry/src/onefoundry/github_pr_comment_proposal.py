"""Offline operator inbox proposal for one exact GitHub PR comment.

The PR head is an injected observation, not a live read performed here. This
module does not dispatch, read credentials, or POST to GitHub.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import Field, ValidationError
from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.github_pr_comment_contract import (
    MAX_COMMENT_CHARS,
    CommentProposal,
    CurrentTarget,
    SavedSource,
    approval_record,
    make_proposal,
)
from onefoundry.role_tool_admission import ToolDenied, _context
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

TOOL_ID = "github_pr_comment_v1"
EVENT_TYPE = "github_pr_comment_proposed"


class ProposalInput(StrictModel):
    source_id: UUID
    number: int = Field(ge=1, le=999_999_999)
    comment: str = Field(min_length=1, max_length=MAX_COMMENT_CHARS)
    idempotency_key: UUID


def _source(connection: Connection, project_id: UUID, source_id: UUID) -> SavedSource | None:
    row = (
        connection.execute(
            text(
                "SELECT s.account_id,s.owner,s.repo,s.branch,s.revision "
                "FROM project_sources s JOIN github_accounts a ON "
                "a.project_id=s.project_id AND a.id=s.account_id "
                "WHERE s.project_id=:project_id AND s.id=:source_id AND s.kind='github' "
                "FOR SHARE OF s"
            ),
            {"project_id": project_id, "source_id": source_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    try:
        return SavedSource(
            project_id=project_id,
            source_id=source_id,
            account_id=row["account_id"],
            owner=row["owner"],
            repo=row["repo"],
            branch=row["branch"],
            saved_revision=row["revision"],
        )
    except (ValidationError, ValueError):
        raise Conflict("saved GitHub source metadata is invalid") from None


def _previous(
    connection: Connection, project_id: UUID, run_id: UUID, idempotency_key: UUID
) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                "SELECT a.id,a.content,a.status,a.proposal_digest,a.target "
                "FROM approval_requests a JOIN events e ON "
                "e.project_id=a.project_id AND e.run_id=a.run_id "
                "AND e.event_type='github_pr_comment_proposed' "
                "AND e.payload->>'approval_id'=a.id::text "
                "AND e.payload->>'proposal_digest'=a.proposal_digest "
                "WHERE a.project_id=:project_id AND a.run_id=:run_id "
                "AND e.payload->>'idempotency_key'=:idempotency_key LIMIT 1"
            ),
            {
                "project_id": project_id,
                "run_id": run_id,
                "idempotency_key": str(idempotency_key),
            },
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _same_proposal(
    previous: dict[str, Any],
    project_id: UUID,
    run_id: UUID,
    work_order_id: UUID,
    actor_id: str,
    source: SavedSource,
    data: ProposalInput,
    observed_head: str,
) -> bool:
    try:
        saved = CommentProposal.model_validate_json(
            json.dumps({**json.loads(previous["content"]), "proposal_digest": previous["proposal_digest"]})
        )
        target, content, digest = approval_record(saved)
    except (TypeError, ValueError, ValidationError):
        return False
    return bool(
        previous["id"] == saved.approval_id
        and previous["target"] == target
        and previous["content"] == content
        and previous["proposal_digest"] == digest
        and saved.project_id == project_id
        and saved.run_id == run_id
        and saved.work_order_id == work_order_id
        and saved.actor_id == actor_id
        and saved.source == source
        and saved.pr_number == data.number
        and saved.head_sha == observed_head
        and saved.comment == data.comment
        and saved.idempotency_key == data.idempotency_key
    )


def propose(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    data: ProposalInput,
    observation: CurrentTarget,
    *,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """Atomically save a proposal and hash-only event; no GitHub operation."""
    settings = gateway.live_text_settings
    if settings is None:
        raise Conflict("role tool settings are unavailable")
    actor = actor_id if actor_id is not None else gateway.actor_id
    with gateway.engine.begin() as connection:
        run = (
            connection.execute(
                text("SELECT profile_id FROM runs WHERE project_id=:project_id AND id=:run_id FOR UPDATE"),
                {"project_id": project_id, "run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise NotFound("run not found")
        context = _context(
            connection,
            project_id,
            run_id,
            run["profile_id"],
            lock=True,
            max_items=settings.role_context_max_items,
        )
        if TOOL_ID not in context.snapshot_tools & context.current_tools:
            raise Conflict("GitHub PR comment is not allowed for this run")
        source = _source(connection, project_id, data.source_id)
        if source is None:
            raise NotFound("saved GitHub source not found")
        if (
            observation.project_id != project_id
            or observation.project_status != "active"
            or observation.source != source
            or observation.pr_number != data.number
        ):
            raise Conflict("trusted PR head observation is not bound to this saved source")
        previous = _previous(connection, project_id, run_id, data.idempotency_key)
        if previous is not None:
            if not _same_proposal(
                previous,
                project_id,
                run_id,
                context.work_order_id,
                actor,
                source,
                data,
                observation.head_sha,
            ):
                raise Conflict("PR comment idempotency key is already bound")
            return {"approval_id": previous["id"], "status": previous["status"]}
        approval_id = uuid4()
        proposal = make_proposal(
            project_id=project_id,
            run_id=run_id,
            work_order_id=context.work_order_id,
            actor_id=actor,
            approval_id=approval_id,
            source=source,
            pr_number=data.number,
            head_sha=observation.head_sha,
            comment=data.comment,
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.github_pr_read_approval_minutes),
            idempotency_key=data.idempotency_key,
        )
        target, content, digest = approval_record(proposal)
        connection.execute(
            text(
                "INSERT INTO approval_requests "
                "(id,project_id,actor_id,work_order_id,run_id,action_type,target,content,"
                "proposal_digest,status) VALUES "
                "(:id,:project_id,:actor_id,:work_order_id,:run_id,'other',:target,:content,"
                ":digest,'pending')"
            ),
            {
                "id": approval_id,
                "project_id": project_id,
                "actor_id": actor,
                "work_order_id": context.work_order_id,
                "run_id": run_id,
                "target": target,
                "content": content,
                "digest": digest,
            },
        )
        gateway._event(
            connection,
            project_id,
            context.work_order_id,
            run_id,
            EVENT_TYPE,
            {
                "approval_id": str(approval_id),
                "proposal_digest": digest,
                "comment_sha256": proposal.comment_sha256,
                "idempotency_key": str(data.idempotency_key),
            },
            actor_id=actor,
        )
        return {"approval_id": approval_id, "status": "pending"}


def pending_source_current(connection: Connection, project_id: UUID, approval: Any) -> bool | None:
    """Approval-time DB source check; PR head still requires an outbound preflight."""
    marker = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:project_id AND run_id=:run_id "
            "AND event_type='github_pr_comment_proposed' "
            "AND payload->>'approval_id'=:approval_id "
            "AND payload->>'proposal_digest'=:proposal_digest"
        ),
        {
            "project_id": project_id,
            "run_id": approval["run_id"],
            "approval_id": str(approval["id"]),
            "proposal_digest": approval["proposal_digest"],
        },
    )
    if marker is None:
        return None
    try:
        proposal = CommentProposal.model_validate_json(
            json.dumps({**json.loads(approval["content"]), "proposal_digest": approval["proposal_digest"]})
        )
        target, content, digest = approval_record(proposal)
        if (
            approval["action_type"] != "other"
            or approval["target"] != target
            or approval["content"] != content
            or approval["proposal_digest"] != digest
            or approval["id"] != proposal.approval_id
            or approval["run_id"] != proposal.run_id
            or proposal.project_id != project_id
            or datetime.now(UTC) >= proposal.expires_at
        ):
            return False
        run_profile_id = connection.scalar(
            text("SELECT profile_id FROM runs WHERE project_id=:project_id AND id=:run_id"),
            {"project_id": project_id, "run_id": proposal.run_id},
        )
        if run_profile_id is None:
            return False
        context = _context(
            connection,
            project_id,
            proposal.run_id,
            run_profile_id,
            lock=True,
            max_items=1,
        )
        if (
            context.work_order_id != proposal.work_order_id
            or TOOL_ID not in context.snapshot_tools & context.current_tools
        ):
            return False
        active = connection.scalar(text("SELECT status FROM projects WHERE id=:id"), {"id": project_id})
        source = _source(connection, project_id, proposal.source.source_id)
        return active == "active" and source == proposal.source
    except (TypeError, ValueError, ValidationError, KeyError, Conflict, ToolDenied, NotFound):
        return False
