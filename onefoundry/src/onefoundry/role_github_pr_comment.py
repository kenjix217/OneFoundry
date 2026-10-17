"""Role-facing, proposal-only GitHub PR comment tool.

The only outbound operation here is a bounded PR metadata GET. Posting belongs
to the separately approved operator dispatch path.
"""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.github_pr_comment_contract import MAX_COMMENT_BYTES, CurrentTarget
from onefoundry.github_pr_comment_dispatch import _credential
from onefoundry.github_pr_comment_proposal import ProposalInput, _source, propose
from onefoundry.github_pr_comment_transport import GitHubCommentTransport
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.role_tool_admission import (
    CALL_ID,
    ToolDenied,
    ToolDependencyFailure,
    ToolInputError,
    ToolOutcomeUnknown,
    ToolTimedOut,
    _argument_bytes,
    _context,
    _db_bounds,
    _event,
    _prior_attempt,
)
from onefoundry.source_github import GitHubUnavailable

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

TOOL_ID = "github_pr_comment_v1"
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string", "format": "uuid"},
        "number": {"type": "integer", "minimum": 1, "maximum": 999_999_999},
        "comment": {"type": "string", "minLength": 1, "maxLength": 16_000},
        "idempotency_key": {"type": "string", "format": "uuid"},
    },
    "required": ["source_id", "number", "comment", "idempotency_key"],
    "additionalProperties": False,
}


def parse_arguments(value: object) -> dict[str, Any]:
    try:
        if (
            not isinstance(value, dict)
            or type(value.get("number")) is not int
            or not isinstance(value.get("source_id"), str)
            or not isinstance(value.get("idempotency_key"), str)
            or not isinstance(value.get("comment"), str)
        ):
            raise ValueError("invalid PR comment argument types")
        data = ProposalInput.model_validate(value)
        encoded = data.comment.encode("utf-8")
        if (
            len(encoded) > MAX_COMMENT_BYTES
            or "\x00" in data.comment
            or "\r" in data.comment
            or contains_sensitive_text(data.comment)
        ):
            raise ValueError("unsafe PR comment")
    except (ValidationError, ValueError, UnicodeError):
        raise ToolInputError("PR comment arguments are invalid or contain unsafe text") from None
    return data.model_dump(mode="json")


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    if CALL_ID.fullmatch(call_id) is None:
        raise ToolInputError("tool call identifier is invalid")
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    deadline = monotonic() + settings.live_text_timeout_seconds
    try:
        with gateway.engine.begin() as connection:
            _db_bounds(connection, deadline)
            context = _context(
                connection,
                project_id,
                run_id,
                profile_id,
                lock=True,
                max_items=settings.role_context_max_items,
            )
            prior = _prior_attempt(connection, project_id, run_id, call_id)
            if prior == "role_tool_started":
                raise ToolOutcomeUnknown("PR comment call outcome is unknown; inspect the run timeline")
            if prior is not None:
                raise ToolDenied("PR comment call already attempted")
            if tool_version != 1 or TOOL_ID not in context.snapshot_tools & context.current_tools:
                _event(
                    gateway,
                    connection,
                    context,
                    "role_tool_denied",
                    {
                        "call_id": call_id,
                        "tool_id": TOOL_ID,
                        "reason": "not_admitted",
                    },
                )
                denied = True
            else:
                denied = False
                try:
                    parsed, encoded = _argument_bytes(
                        arguments, settings.live_text_max_prompt_bytes, parse_arguments
                    )
                    source = _source(connection, project_id, UUID(parsed["source_id"]))
                    if source is None:
                        raise ToolInputError("saved GitHub source not found")
                except ToolInputError:
                    _event(
                        gateway,
                        connection,
                        context,
                        "role_tool_denied",
                        {
                            "call_id": call_id,
                            "tool_id": TOOL_ID,
                            "reason": "invalid_arguments",
                        },
                    )
                    denied = True
                else:
                    _event(
                        gateway,
                        connection,
                        context,
                        "role_tool_started",
                        {
                            "call_id": call_id,
                            "tool_id": TOOL_ID,
                            "version": 1,
                            "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
                            "argument_bytes": len(encoded),
                            "timeout_seconds": settings.live_text_timeout_seconds,
                        },
                    )
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in ("55P03", "57014"):
            raise ToolTimedOut("PR comment admission timed out") from None
        raise ToolDependencyFailure("PR comment admission failed") from None
    if denied:
        raise ToolDenied("PR comment call was denied")
    assert source is not None

    proposal_saved = False
    try:
        credential = _credential(gateway, source.account_id)
        head = GitHubCommentTransport(settings).current_head(credential, source, parsed["number"])
        if monotonic() > deadline:
            raise ToolTimedOut("PR comment observation timed out")
        observation = CurrentTarget(
            project_id=project_id,
            project_status="active",
            source=source,
            pr_number=parsed["number"],
            head_sha=head,
        )
        result = propose(
            gateway,
            project_id,
            run_id,
            ProposalInput.model_validate(parsed),
            observation,
            actor_id=f"profile:{profile_id}",
        )
        proposal_saved = True
        output = {"approval_id": str(result["approval_id"]), "status": result["status"]}
        encoded_result = json.dumps(output, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(encoded_result).hexdigest()
        with gateway.engine.begin() as connection:
            _db_bounds(connection, deadline)
            current = _context(
                connection,
                project_id,
                run_id,
                profile_id,
                lock=True,
                max_items=settings.role_context_max_items,
            )
            _event(
                gateway,
                connection,
                current,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "status": "done",
                    "result_sha256": digest,
                    "result_bytes": len(encoded_result),
                },
            )
        return {
            "call_id": call_id,
            "tool_id": TOOL_ID,
            "version": 1,
            "status": "done",
            "result": output,
            "result_sha256": digest,
        }
    except Exception as error:
        if proposal_saved:
            raise ToolOutcomeUnknown("PR comment proposal may have been saved; inspect the inbox") from None
        reason = (
            "timeout"
            if isinstance(error, ToolTimedOut)
            else "target_changed"
            if isinstance(error, Conflict)
            else "observation_failed"
        )
        try:
            with gateway.engine.begin() as connection:
                _event(
                    gateway,
                    connection,
                    context,
                    "role_tool_finished",
                    {
                        "call_id": call_id,
                        "tool_id": TOOL_ID,
                        "status": "failed",
                        "reason": reason,
                    },
                )
        except Exception:
            raise ToolOutcomeUnknown("PR comment failure could not be recorded") from None
        if isinstance(error, ToolTimedOut):
            raise
        if isinstance(error, (Conflict, NotFound)):
            raise ToolDenied("PR comment target changed before proposal") from None
        if isinstance(error, (GitHubUnavailable, Unavailable)):
            raise ToolDependencyFailure("GitHub PR observation unavailable") from None
        raise ToolDependencyFailure("PR comment proposal failed") from None
