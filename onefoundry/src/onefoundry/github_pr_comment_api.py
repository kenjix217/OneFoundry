"""Operator HTTP boundary for an inbox-approved GitHub PR issue comment."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import Field, ValidationError
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.github_pr_comment_contract import ContractError, CurrentTarget
from onefoundry.github_pr_comment_dispatch import _credential, dispatch, reconcile, status
from onefoundry.github_pr_comment_proposal import ProposalInput, _source, propose
from onefoundry.github_pr_comment_transport import GitHubCommentTransport
from onefoundry.role_tool_admission import ToolDenied, _context
from onefoundry.source_github import GitHubUnavailable
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute
from onefoundry.workflow_schemas import StrictModel


class CommentProposalRequest(StrictModel):
    run_id: UUID
    source_id: UUID
    number: int = Field(ge=1, le=999_999_999)
    comment: str = Field(min_length=1, max_length=16_000)
    idempotency_key: UUID


def _observed_target(
    gateway: ToolGateway, project_id: UUID, run_id: UUID, source_id: UUID, number: int
) -> CurrentTarget:
    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("GitHub PR comment settings unavailable")
    with gateway.engine.connect() as connection:
        profile_id = connection.scalar(
            text("SELECT profile_id FROM runs WHERE project_id=:project_id AND id=:run_id"),
            {"project_id": project_id, "run_id": run_id},
        )
        if profile_id is None:
            raise NotFound("run not found")
        context = _context(
            connection,
            project_id,
            run_id,
            profile_id,
            lock=False,
            max_items=settings.role_context_max_items,
        )
        if "github_pr_comment_v1" not in context.snapshot_tools & context.current_tools:
            raise ToolDenied("GitHub PR comment is not allowed for this run")
        project_status = connection.scalar(
            text("SELECT status FROM projects WHERE id=:id"), {"id": project_id}
        )
        if project_status is None:
            raise NotFound("project not found")
        if project_status != "active":
            raise Conflict("project is not active")
        saved_source = _source(connection, project_id, source_id)
    if saved_source is None:
        raise NotFound("saved GitHub source not found")
    credential = _credential(gateway, saved_source.account_id)
    try:
        head = GitHubCommentTransport(settings).current_head(credential, saved_source, number)
    except (GitHubUnavailable, OSError, ValueError):
        raise Unavailable("GitHub PR observation unavailable") from None
    return CurrentTarget(
        project_id=project_id,
        project_status="active",
        source=saved_source,
        pr_number=number,
        head_sha=head,
    )


def make_github_pr_comment_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/github-pr-comments")

    @router.post("", status_code=201)
    def create(project_id: UUID, data: CommentProposalRequest) -> dict[str, Any]:
        try:
            proposal = ProposalInput.model_validate(data.model_dump(exclude={"run_id"}))
            observation = execute(
                lambda: _observed_target(gateway, project_id, data.run_id, data.source_id, data.number)
            )
            return execute(lambda: propose(gateway, project_id, data.run_id, proposal, observation))
        except (ValidationError, ValueError):
            raise HTTPException(422, "GitHub PR comment proposal is invalid") from None

    @router.get("/{approval_id}")
    def read(project_id: UUID, approval_id: UUID) -> dict[str, Any]:
        return execute(lambda: status(gateway, project_id, approval_id))

    @router.post("/{approval_id}/dispatch")
    def send(project_id: UUID, approval_id: UUID) -> dict[str, Any]:
        try:
            return execute(lambda: dispatch(gateway, project_id, approval_id))
        except ContractError:
            raise HTTPException(409, "approved GitHub PR comment binding is stale") from None

    @router.post("/{approval_id}/reconcile")
    def check(project_id: UUID, approval_id: UUID) -> dict[str, Any]:
        try:
            return execute(lambda: reconcile(gateway, project_id, approval_id))
        except ContractError:
            raise HTTPException(409, "GitHub PR comment receipt binding is stale") from None

    return router
