"""Admission and durable receipt for one role-owned private text artifact."""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from onefoundry.artifact_workspace import MAX_ARTIFACT_BYTES, _validate_content
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.role_tool_admission import (
    CALL_ID,
    ToolDenied,
    ToolInputError,
    ToolOutcomeUnknown,
    _argument_bytes,
    _context,
    _db_bounds,
    _event,
    _prior_attempt,
)
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


TOOL_ID = "write_text_artifact_v1"
_MIME = {"text": "text/plain", "markdown": "text/markdown", "csv": "text/csv"}
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(_MIME)},
        "content": {"type": "string", "maxLength": MAX_ARTIFACT_BYTES},
    },
    "required": ["kind", "content"],
    "additionalProperties": False,
}


class Arguments(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["text", "markdown", "csv"]
    content: str


def _parse_arguments(value: object) -> dict[str, Any]:
    try:
        parsed = Arguments.model_validate(value, strict=True)
        content = parsed.content.encode("utf-8")
        _validate_content(content, parsed.kind)
    except (ValidationError, UnicodeError, ValueError):
        raise ToolInputError("artifact arguments are invalid or contain unsafe text") from None
    return {"kind": parsed.kind, "content": parsed.content}


def _metadata(artifact_id: UUID, kind: str, sha256: str, byte_size: int) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact_id),
        "kind": kind,
        "mime_type": _MIME[kind],
        "sha256": sha256,
        "byte_size": byte_size,
    }


def _mark_unknown(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    work_order_id: UUID,
    call_id: str,
    artifact_id: UUID,
    reason: str,
) -> None:
    """Seal a committed intent; a failed seal leaves pending as a no-retry marker."""
    with gateway.engine.begin() as connection:
        changed = connection.execute(
            text(
                "UPDATE workspace_artifacts SET status='unknown',finished_at=clock_timestamp() "
                "WHERE project_id=:project_id AND run_id=:run_id AND id=:id AND status='pending'"
            ),
            {"project_id": project_id, "run_id": run_id, "id": artifact_id},
        )
        if changed.rowcount != 1:
            return
        gateway._event(
            connection,
            project_id,
            work_order_id,
            run_id,
            "role_tool_finished",
            {
                "call_id": call_id,
                "tool_id": TOOL_ID,
                "status": "unknown",
                "artifact_id": str(artifact_id),
                "reason": reason,
            },
            actor_id=f"profile:{profile_id}",
        )


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    """Commit an intent before writing bytes, then publish only checked metadata."""
    if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None or contains_sensitive_text(call_id):
        raise ToolInputError("tool call identifier is invalid")
    if type(tool_version) is not int or tool_version != 1:
        raise ToolDenied("artifact tool version is not admitted")
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    deadline = monotonic() + settings.live_text_timeout_seconds
    artifact_id = uuid4()
    context = None
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
            if prior is not None:
                if prior == "role_tool_started":
                    raise ToolOutcomeUnknown("artifact call outcome is unknown; inspect the run timeline")
                raise ToolDenied("artifact call already attempted; inspect the run timeline")
            if TOOL_ID not in context.snapshot_tools & context.current_tools:
                raise ToolDenied("artifact write is not admitted for this run")
            # JSON escaping may expand valid UTF-8 text beyond the live prompt
            # budget; artifact bytes have their own 64 KiB storage boundary.
            parsed, encoded = _argument_bytes(arguments, MAX_ARTIFACT_BYTES * 4, _parse_arguments)
            content = parsed["content"].encode("utf-8")
            kind = parsed["kind"]
            sha256 = hashlib.sha256(content).hexdigest()
            relative_path = f"{project_id}/{run_id}/{artifact_id}.txt"
            connection.execute(
                text(
                    "INSERT INTO workspace_artifacts "
                    "(id,project_id,run_id,actor_id,call_id,kind,mime_type,relative_path,"
                    "sha256,byte_size,status) VALUES "
                    "(:id,:project_id,:run_id,:actor_id,:call_id,:kind,:mime_type,:relative_path,"
                    ":sha256,:byte_size,'pending')"
                ),
                {
                    "id": artifact_id,
                    "project_id": project_id,
                    "run_id": run_id,
                    "actor_id": f"profile:{profile_id}",
                    "call_id": call_id,
                    "kind": kind,
                    "mime_type": _MIME[kind],
                    "relative_path": relative_path,
                    "sha256": sha256,
                    "byte_size": len(content),
                },
            )
            result = _metadata(artifact_id, kind, sha256, len(content))
            serialized_result = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
            result_sha256 = hashlib.sha256(serialized_result).hexdigest()
            _event(
                gateway,
                connection,
                context,
                "role_tool_started",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "version": 1,
                    "artifact_id": str(artifact_id),
                    "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
                    "argument_bytes": len(encoded),
                    "content_sha256": sha256,
                    "content_bytes": len(content),
                },
            )
    except DBAPIError:
        raise ToolOutcomeUnknown("artifact intent could not be confirmed") from None

    assert context is not None
    try:
        saved = gateway._artifact_workspace().write(
            project_id,
            run_id,
            artifact_id,
            kind=kind,
            mime_type=_MIME[kind],
            content=content,
        )
        if saved.relative_path != relative_path or saved.sha256 != sha256 or saved.byte_size != len(content):
            raise ToolOutcomeUnknown("artifact storage receipt does not match the committed intent")
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
                raise ToolDenied("artifact write permission changed before publication")
            updated = connection.execute(
                text(
                    "UPDATE workspace_artifacts SET status='ready',finished_at=clock_timestamp() "
                    "WHERE project_id=:project_id AND run_id=:run_id AND id=:id AND status='pending' "
                    "AND sha256=:sha256 AND byte_size=:byte_size AND relative_path=:relative_path"
                ),
                {
                    "project_id": project_id,
                    "run_id": run_id,
                    "id": artifact_id,
                    "sha256": sha256,
                    "byte_size": len(content),
                    "relative_path": relative_path,
                },
            )
            if updated.rowcount != 1:
                raise ToolOutcomeUnknown("artifact owner metadata changed before publication")
            _event(
                gateway,
                connection,
                current,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "status": "done",
                    "artifact_id": str(artifact_id),
                    "result_sha256": result_sha256,
                    "result_bytes": len(serialized_result),
                    "content_sha256": sha256,
                    "content_bytes": len(content),
                },
            )
        return {
            "call_id": call_id,
            "tool_id": TOOL_ID,
            "version": 1,
            "status": "done",
            "result": result,
            "result_sha256": result_sha256,
        }
    except Exception as error:
        reason = "permission_changed" if isinstance(error, ToolDenied) else "storage_or_owner_uncertain"
        try:
            _mark_unknown(
                gateway,
                project_id,
                run_id,
                profile_id,
                context.work_order_id,
                call_id,
                artifact_id,
                reason,
            )
        except Exception:
            raise ToolOutcomeUnknown(
                "artifact outcome and terminal owner record could not be confirmed"
            ) from None
        raise ToolOutcomeUnknown("artifact outcome is unknown; inspect the run timeline") from None
