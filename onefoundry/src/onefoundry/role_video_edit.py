"""Registered editing tools admit exact active run/profile permissions and record receipts."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text

from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.role_tool_admission import CALL_ID, ToolDenied, ToolInputError, _context, _db_bounds, _event
from onefoundry.video_edit_contract import EditRequest, digest
from onefoundry.video_edit_jobs import VideoEditJobs, public_job
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

EDIT = "edit_video_v1"
STATUS = "video_edit_status_v1"
CANCEL = "cancel_video_edit_v1"
TOOL_IDS = frozenset({EDIT, STATUS, CANCEL})


class JobRequest(StrictModel):
    job_id: UUID


def descriptors() -> list[dict[str, Any]]:
    specifications: list[tuple[str, type[StrictModel], str]] = [
        (
            EDIT,
            EditRequest,
            "Queue a real bounded local edit from authorized project asset IDs. "
            "A queued job is not a rendered artifact. Inspect video_edit_status_v1 for artifact and QC.",
        ),
        (STATUS, JobRequest, "Read your work order video job status and exact artifact/QC receipt."),
        (CANCEL, JobRequest, "Cancel your queued or running media edit."),
    ]
    return [
        {"id": key, "version": 1, "parameters": schema.model_json_schema(), "description": description}
        for key, schema, description in specifications
    ]


def invoke(
    gateway: ToolGateway,
    project: UUID,
    run: UUID,
    profile: UUID,
    call_id: str,
    tool_id: str,
    arguments: object,
    version: int,
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None or not settings.video_edit_enabled:
        raise ToolDenied("local video editing is disabled")
    if version != 1 or not CALL_ID.fullmatch(call_id) or contains_sensitive_text(call_id):
        raise ToolInputError("invalid video edit call identity")
    try:
        parsed = (EditRequest if tool_id == EDIT else JobRequest).model_validate(arguments)
    except ValidationError:
        raise ToolInputError("invalid video edit arguments") from None
    args_sha = digest(parsed.model_dump(mode="json"))
    jobs = VideoEditJobs(gateway)
    with gateway.engine.begin() as connection:
        _db_bounds(connection, monotonic() + settings.live_text_timeout_seconds)
        context = _context(
            connection, project, run, profile, lock=True, max_items=settings.role_context_max_items
        )
        if tool_id not in context.snapshot_tools & context.current_tools:
            raise ToolDenied("video edit tool is not admitted for this active run")
        prior = connection.execute(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND run_id=:r "
                "AND event_type='role_tool_finished' AND payload->>'call_id'=:call ORDER BY "
                "created_at LIMIT 1"
            ),
            {"p": project, "r": run, "call": call_id},
        ).scalar_one_or_none()
        if prior is not None:
            if prior.get("tool_id") != tool_id or prior.get("arguments_sha256") != args_sha:
                raise ToolDenied("video tool call identity changed")
            result = prior["result"]
        else:
            _event(
                gateway,
                connection,
                context,
                "role_tool_started",
                {"call_id": call_id, "tool_id": tool_id, "arguments_sha256": args_sha},
            )
            if isinstance(parsed, EditRequest):
                result = jobs.queue(connection, context, parsed)
            else:
                row = jobs.get(project, parsed.job_id, connection)
                if row["work_order_id"] != context.work_order_id or row["profile_id"] != profile:
                    raise ToolDenied("video edit job is outside this run's work order/profile")
                result = jobs.cancel(connection, row) if tool_id == CANCEL else public_job(row)
            _event(
                gateway,
                connection,
                context,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": tool_id,
                    "arguments_sha256": args_sha,
                    "status": "done",
                    "result": result,
                    "result_sha256": digest(result),
                },
            )
    return {
        "call_id": call_id,
        "tool_id": tool_id,
        "version": 1,
        "status": "done",
        "result": result,
        "result_sha256": digest(result),
    }
