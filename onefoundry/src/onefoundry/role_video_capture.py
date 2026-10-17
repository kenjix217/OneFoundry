"""Request operator-selected local recording; this tool never starts a browser."""

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator
from sqlalchemy import text

from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.role_tool_admission import CALL_ID, ToolDenied, ToolInputError, _context, _db_bounds, _event
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

TOOL_ID = "request_video_capture_v1"
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "caption": {"type": "string", "minLength": 1, "maxLength": 240},
    },
    "required": ["title", "caption"],
    "additionalProperties": False,
}


class CaptureRequest(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    caption: str = Field(min_length=1, max_length=240)

    @field_validator("title", "caption")
    @classmethod
    def safe_text(cls, value: str) -> str:
        value = local_text(value)
        if contains_sensitive_text(value):
            raise ValueError("recording text contains sensitive data")
        return value


def invoke(
    gateway: "ToolGateway",
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None or not settings.media_capture_enabled:
        raise ToolDenied("operator-selected local recording is disabled")
    if tool_version != 1 or not CALL_ID.fullmatch(call_id) or contains_sensitive_text(call_id):
        raise ToolInputError("invalid recording request identity")
    parsed = CaptureRequest.model_validate(arguments)
    capture_id = uuid5(NAMESPACE_URL, f"onefoundry:video:{project_id}:{run_id}:{call_id}")
    result = {
        "capture_id": str(capture_id),
        "run_id": str(run_id),
        "profile_id": str(profile_id),
        **parsed.model_dump(),
        "status": "waiting_for_operator",
        "next_step": "Operator selects an approved app window, records, edits and saves locally. "
        "No recording or agent turn was launched.",
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    response = {
        "call_id": call_id,
        "tool_id": TOOL_ID,
        "version": 1,
        "status": "done",
        "result": result,
        "result_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    with gateway.engine.begin() as connection:
        _db_bounds(connection, monotonic() + settings.live_text_timeout_seconds)
        context = _context(
            connection, project_id, run_id, profile_id, lock=True, max_items=settings.role_context_max_items
        )
        if TOOL_ID not in context.snapshot_tools & context.current_tools:
            raise ToolDenied("recording request is not admitted for this run")
        if (
            connection.scalar(
                text(
                    "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND id=:r AND status='running'"
                ),
                {"p": project_id, "r": run_id},
            )
            is None
        ):
            raise ToolDenied("recording requests require an active team-owned attempt")
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND run_id=:r "
                "AND event_type='media_capture_requested' AND payload->>'capture_id'=:id"
            ),
            {"p": project_id, "r": run_id, "id": str(capture_id)},
        )
        if prior is not None:
            if dict(prior) != result:
                raise ToolDenied("recording request identity changed")
            return response
        count = connection.scalar(
            text(
                "SELECT count(*) FROM events WHERE project_id=:p AND work_order_id=:o "
                "AND event_type='media_capture_requested'"
            ),
            {"p": project_id, "o": context.work_order_id},
        )
        if count is not None and count >= settings.team_max_assignments_per_order:
            raise ToolDenied("this recording task reached its configured request bound")
        _event(gateway, connection, context, "role_tool_started", {"call_id": call_id, "tool_id": TOOL_ID})
        _event(gateway, connection, context, "media_capture_requested", result)
        _event(
            gateway,
            connection,
            context,
            "role_tool_finished",
            {"call_id": call_id, "tool_id": TOOL_ID, "status": "done", "result": result},
        )
    return response
