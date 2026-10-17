"""Approval-bound GitHub PR read for one saved source and one role run."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.github_pr_transport import read_pull_request, validate_pull_request
from onefoundry.role_tool_admission import (
    RunContext,
    ToolDenied,
    ToolDependencyFailure,
    ToolOutcomeUnknown,
    ToolOutputRejected,
    ToolTimedOut,
    _argument_bytes,
    _context,
    _db_bounds,
    _event,
    _prior_attempt,
    _reject_sensitive_output,
)
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

TOOL_ID = "github_pr_read_v1"
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"approval_id": {"type": "string", "format": "uuid"}},
    "required": ["approval_id"],
    "additionalProperties": False,
}


class Proposal(StrictModel):
    source_id: UUID
    number: int = Field(ge=1, le=999999999)
    idempotency_key: UUID


class Arguments(StrictModel):
    approval_id: UUID


def parse_arguments(value: object) -> dict[str, Any]:
    return Arguments.model_validate(value).model_dump(mode="json")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(content: str, target: str) -> str:
    return hashlib.sha256(
        _canonical({"action_type": "other", "target": target, "content": content}).encode()
    ).hexdigest()


def _source(connection: Connection, project_id: UUID, source_id: UUID) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                "SELECT s.account_id,s.owner,s.repo,s.branch,s.revision,"
                "s.created_at AS source_created_at,a.created_at AS account_created_at "
                "FROM project_sources s JOIN github_accounts a ON "
                "a.project_id=s.project_id AND a.id=s.account_id "
                "WHERE s.project_id=:project_id AND s.id=:source_id AND s.kind='github'"
            ),
            {"project_id": project_id, "source_id": source_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _exact_source(source_id: UUID, row: dict[str, Any]) -> dict[str, str]:
    return {
        "source_id": str(source_id),
        "account_id": str(row["account_id"]),
        "owner": row["owner"],
        "repo": row["repo"],
        "branch": row["branch"],
        "saved_revision": row["revision"],
        "source_created_at": row["source_created_at"].isoformat(),
        "account_created_at": row["account_created_at"].isoformat(),
    }


def propose(gateway: ToolGateway, project_id: UUID, run_id: UUID, data: Proposal) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None:
        raise Conflict("role tool settings are unavailable")
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
            raise Conflict("GitHub PR read is not allowed for this run")
        source = _source(connection, project_id, data.source_id)
        if source is None:
            raise NotFound("saved GitHub source not found")
        exact = _exact_source(data.source_id, source)
        previous = (
            connection.execute(
                text(
                    "SELECT a.id,a.content,a.status,a.proposal_digest,a.target FROM approval_requests a "
                    "JOIN events e ON e.project_id=a.project_id AND e.run_id=a.run_id "
                    "AND e.event_type='github_pr_read_proposed' "
                    "AND e.payload->>'approval_id'=a.id::text "
                    "WHERE a.project_id=:project_id AND a.run_id=:run_id "
                    "AND e.payload->>'idempotency_key'=:key LIMIT 1"
                ),
                {"project_id": project_id, "run_id": run_id, "key": str(data.idempotency_key)},
            )
            .mappings()
            .one_or_none()
        )
        if previous is not None:
            try:
                prior = json.loads(previous["content"])
            except ValueError:
                raise Conflict("saved PR read proposal is invalid") from None
            if (
                prior.get("source") != exact
                or prior.get("number") != data.number
                or prior.get("idempotency_key") != str(data.idempotency_key)
            ):
                raise Conflict("PR read idempotency key is already bound")
            return {"approval_id": previous["id"], "status": previous["status"]}
        expires_at = datetime.now(UTC) + timedelta(minutes=settings.github_pr_read_approval_minutes)
        content = _canonical(
            {
                "kind": TOOL_ID,
                "project_id": str(project_id),
                "run_id": str(run_id),
                "source": exact,
                "number": data.number,
                "idempotency_key": str(data.idempotency_key),
                "expires_at": expires_at.isoformat(),
            }
        )
        target = f"Read GitHub PR #{data.number} in {source['owner']}/{source['repo']}"
        digest = _digest(content, target)
        approval_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO approval_requests (id,project_id,actor_id,work_order_id,run_id,"
                "action_type,target,content,proposal_digest,status) VALUES "
                "(:id,:project_id,:actor_id,:work_order_id,:run_id,'other',:target,:content,:digest,'pending')"
            ),
            {
                "id": approval_id,
                "project_id": project_id,
                "actor_id": gateway.actor_id,
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
            "github_pr_read_proposed",
            {
                "approval_id": str(approval_id),
                "proposal_digest": digest,
                "source_id": str(data.source_id),
                "number": data.number,
                "idempotency_key": str(data.idempotency_key),
            },
        )
        return {"approval_id": approval_id, "status": "pending"}


def _binding(connection: Connection, context: RunContext, approval_id: UUID) -> dict[str, Any]:
    approval = (
        connection.execute(
            text(
                "SELECT id,run_id,work_order_id,status,action_type,target,content,proposal_digest "
                "FROM approval_requests WHERE project_id=:project_id AND id=:id FOR UPDATE"
            ),
            {"project_id": context.project_id, "id": approval_id},
        )
        .mappings()
        .one_or_none()
    )
    if (
        approval is None
        or approval["run_id"] != context.run_id
        or approval["work_order_id"] != context.work_order_id
    ):
        raise ToolDenied("PR read approval is not bound to this run")
    if approval["status"] != "approved" or approval["action_type"] != "other":
        raise ToolDenied("PR read has not been approved")
    if _digest(approval["content"], approval["target"]) != approval["proposal_digest"]:
        raise ToolDenied("PR read proposal digest changed")
    marker = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:project_id AND run_id=:run_id "
            "AND event_type='github_pr_read_proposed' AND payload->>'approval_id'=:id "
            "AND payload->>'proposal_digest'=:digest"
        ),
        {
            "project_id": context.project_id,
            "run_id": context.run_id,
            "id": str(approval_id),
            "digest": approval["proposal_digest"],
        },
    )
    if marker is None:
        raise ToolDenied("PR read proposal has no gateway event")
    try:
        intent = json.loads(approval["content"])
        source_id = UUID(intent["source"]["source_id"])
        expires_at = datetime.fromisoformat(intent["expires_at"])
        number = intent["number"]
    except (TypeError, ValueError, KeyError):
        raise ToolDenied("PR read proposal is invalid") from None
    if (
        intent.get("kind") != TOOL_ID
        or intent.get("project_id") != str(context.project_id)
        or intent.get("run_id") != str(context.run_id)
        or type(number) is not int
        or number < 1
        or expires_at.tzinfo is None
        or datetime.now(UTC) >= expires_at
    ):
        raise ToolDenied("PR read proposal is stale or invalid")
    source = _source(connection, context.project_id, source_id)
    if source is None or _exact_source(source_id, source) != intent["source"]:
        raise ToolDenied("saved GitHub source changed")
    return {"source": source, "number": number, "approval_id": approval_id}


def pending_source_current(connection: Connection, project_id: UUID, approval: Any) -> bool | None:
    """Return None for another approval kind, or whether this PR proposal is still exact."""
    marker = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:project_id AND run_id=:run_id "
            "AND event_type='github_pr_read_proposed' AND payload->>'approval_id'=:id "
            "AND payload->>'proposal_digest'=:digest"
        ),
        {
            "project_id": project_id,
            "run_id": approval["run_id"],
            "id": str(approval["id"]),
            "digest": approval["proposal_digest"],
        },
    )
    if marker is None:
        return None
    try:
        intent = json.loads(approval["content"])
        source_id = UUID(intent["source"]["source_id"])
        expiry = datetime.fromisoformat(intent["expires_at"])
        source = _source(connection, project_id, source_id)
        return bool(
            approval["action_type"] == "other"
            and _digest(approval["content"], approval["target"]) == approval["proposal_digest"]
            and intent["kind"] == TOOL_ID
            and intent["project_id"] == str(project_id)
            and intent["run_id"] == str(approval["run_id"])
            and expiry.tzinfo is not None
            and datetime.now(UTC) < expiry
            and source is not None
            and _exact_source(source_id, source) == intent["source"]
        )
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    deadline = monotonic() + settings.live_text_timeout_seconds
    with gateway.engine.begin() as connection:
        _db_bounds(connection, deadline)
        context = _context(
            connection, project_id, run_id, profile_id, lock=True, max_items=settings.role_context_max_items
        )
        if _prior_attempt(connection, project_id, run_id, call_id) is not None:
            raise ToolOutcomeUnknown("PR read call already attempted; inspect the run timeline")
        if tool_version != 1 or TOOL_ID not in context.snapshot_tools & context.current_tools:
            raise ToolDenied("PR read is not admitted for this run")
        parsed, encoded = _argument_bytes(arguments, settings.live_text_max_prompt_bytes, parse_arguments)
        approval_id = UUID(parsed["approval_id"])
        binding = _binding(connection, context, approval_id)
        consumed = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:project_id AND run_id=:run_id "
                "AND event_type='github_pr_read_started' AND payload->>'approval_id'=:id"
            ),
            {"project_id": project_id, "run_id": run_id, "id": str(approval_id)},
        )
        if consumed is not None:
            raise ToolDenied("PR read approval was already used")
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
            },
        )
        _event(
            gateway,
            connection,
            context,
            "github_pr_read_started",
            {
                "call_id": call_id,
                "approval_id": str(approval_id),
                "saved_revision": binding["source"]["revision"],
            },
        )
    result_ready = False
    try:
        from pathlib import Path

        from onefoundry.model_secrets import ModelSecretStore

        source = binding["source"]
        credential = ModelSecretStore(str(Path(settings.model_secret_root) / "github")).read(
            source["account_id"]
        )
        result = read_pull_request(
            credential,
            settings,
            source["owner"],
            source["repo"],
            binding["number"],
            source["revision"],
            source["branch"],
            deadline,
        )
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
            if TOOL_ID not in current.snapshot_tools & current.current_tools:
                raise ToolDenied("PR read permission changed")
            _binding(connection, current, approval_id)
            validate_pull_request(result)
            _reject_sensitive_output(result)
            encoded_result = _canonical(result).encode()
            if len(encoded_result) > settings.live_text_max_output_bytes:
                raise ToolOutputRejected("PR read result exceeds output limit")
            if monotonic() > deadline:
                raise ToolTimedOut("PR read timed out")
            result_ready = True
            _event(
                gateway,
                connection,
                current,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "status": "done",
                    "result_sha256": hashlib.sha256(encoded_result).hexdigest(),
                    "result_bytes": len(encoded_result),
                },
            )
        return {
            "call_id": call_id,
            "tool_id": TOOL_ID,
            "version": 1,
            "status": "done",
            "result": result,
            "result_sha256": hashlib.sha256(encoded_result).hexdigest(),
        }
    except Exception as error:
        if result_ready:
            raise ToolOutcomeUnknown("PR read outcome could not be recorded") from None
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
                        "reason": "read_or_policy_failure",
                    },
                )
        except Exception:
            raise ToolOutcomeUnknown("PR read failure could not be recorded") from None
        if isinstance(error, (ToolDenied, ToolOutputRejected, ToolTimedOut, ToolDependencyFailure)):
            raise
        raise ToolDependencyFailure("approved PR read failed") from None
