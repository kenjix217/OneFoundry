"""Role admission for one approved static browser fixture.

The role supplies only a fixture ID. The browser job owner binds the saved
source and exact host manifest; this tool never accepts browser source bytes.
"""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.role_tool_admission import (
    RunContext,
    ToolDenied,
    ToolDependencyFailure,
    ToolInputError,
    ToolOutcomeUnknown,
    ToolOutputRejected,
    ToolTimedOut,
    _argument_bytes,
    _context,
    _db_bounds,
    _event,
    _reject_sensitive_output,
)
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.browser_static_jobs import BrowserStaticJobStore
    from onefoundry.tool_gateway import ToolGateway

TOOL_ID = "browser_static_inspect_v1"
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"fixture_id": {"type": "string", "format": "uuid"}},
    "required": ["fixture_id"],
    "additionalProperties": False,
}
DESCRIPTION = "Queue inspection of one operator-approved static browser fixture for this run."
_KEY_NAMESPACE = UUID("6d32ca44-1fbb-4a2d-a5e1-eb868874f255")


class Arguments(StrictModel):
    fixture_id: UUID


def parse_arguments(value: object) -> dict[str, Any]:
    try:
        parsed = Arguments.model_validate(value)
    except ValidationError as error:
        raise ValueError("browser inspection arguments are invalid") from error
    if parsed.fixture_id.int == 0:
        raise ValueError("browser fixture ID is invalid")
    return parsed.model_dump(mode="json")


def role_admitted(context: RunContext) -> bool:
    return (
        context.current_role_kind == "builtin"
        and context.current_role_name in {"Researcher", "QA"}
        and context.snapshot_role_name == context.current_role_name
    )


def descriptor() -> dict[str, Any]:
    return {"id": TOOL_ID, "version": 1, "description": DESCRIPTION, "parameters": PARAMETERS}


def _call_key(project_id: UUID, run_id: UUID, call_id: str) -> UUID:
    return uuid5(_KEY_NAMESPACE, f"{project_id}:{run_id}:{call_id}")


def _prior(connection: Any, context: RunContext, call_id: str) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                "SELECT event_type,payload,actor_id FROM events "
                "WHERE project_id=:project_id AND run_id=:run_id "
                "AND event_type IN ('role_tool_started','role_tool_denied','role_tool_finished') "
                "AND payload->>'call_id'=:call_id ORDER BY created_at,id LIMIT 1"
            ),
            {"project_id": context.project_id, "run_id": context.run_id, "call_id": call_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _result(
    jobs: BrowserStaticJobStore, project_id: UUID, run_id: UUID, job: dict[str, Any]
) -> dict[str, Any]:
    status = str(job["status"])
    result: dict[str, Any] = {"job_id": str(job["id"]), "status": status}
    if status == "done":
        evidence = jobs.evidence(project_id, run_id, job["id"])
        result.update(
            {
                "title": evidence["title"],
                "text": evidence["text"],
                "blocked_request_count": evidence["blocked_request_count"],
            }
        )
    elif status in {"failed", "unknown"}:
        result["reason_code"] = job["reason_code"]
    elif status != "queued":
        raise ToolOutcomeUnknown("browser inspection job is not ready")
    _reject_sensitive_output(result)
    return result


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
    jobs = gateway.browser_static_jobs
    if settings is None or jobs is None:
        raise ToolDenied("browser inspection owner is unavailable")
    parsed: dict[str, Any] | None = None
    encoded: bytes | None = None
    prior: dict[str, Any] | None = None
    try:
        with gateway.engine.begin() as connection:
            _db_bounds(connection, monotonic() + settings.live_text_timeout_seconds)
            context = _context(
                connection,
                project_id,
                run_id,
                profile_id,
                lock=True,
                max_items=settings.role_context_max_items,
            )
            prior = _prior(connection, context, call_id)
            allowed = TOOL_ID in context.snapshot_tools & context.current_tools and role_admitted(context)
            if not allowed or tool_version != 1:
                if prior is None:
                    _event(
                        gateway,
                        connection,
                        context,
                        "role_tool_denied",
                        {
                            "call_id": call_id,
                            "tool_id": TOOL_ID,
                            "reason": "version_mismatch" if tool_version != 1 else "not_admitted",
                        },
                    )
                denied = True
            else:
                denied = False
                try:
                    parsed, encoded = _argument_bytes(
                        arguments, settings.live_text_max_prompt_bytes, parse_arguments
                    )
                except ToolInputError:
                    if prior is None:
                        _event(
                            gateway,
                            connection,
                            context,
                            "role_tool_denied",
                            {"call_id": call_id, "tool_id": TOOL_ID, "reason": "invalid_arguments"},
                        )
                    denied = True
                if not denied and prior is None:
                    assert parsed is not None and encoded is not None
                    # Approval and same-run ownership are checked before intent,
                    # then checked again by the job owner before staging.
                    try:
                        jobs.fixtures.approved(project_id, run_id, UUID(parsed["fixture_id"]))
                    except (Conflict, NotFound):
                        _event(
                            gateway,
                            connection,
                            context,
                            "role_tool_denied",
                            {"call_id": call_id, "tool_id": TOOL_ID, "reason": "fixture_not_approved"},
                        )
                        denied = True
                    if not denied:
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
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in ("55P03", "57014"):
            raise ToolTimedOut("browser inspection admission timed out") from None
        raise ToolDependencyFailure("browser inspection admission dependency failed") from None
    if denied:
        raise ToolDenied("browser inspection call was denied")
    assert parsed is not None and encoded is not None
    fixture_id = UUID(parsed["fixture_id"])
    if prior is not None:
        payload = prior["payload"]
        if (
            prior["event_type"] == "role_tool_denied"
            or prior["actor_id"] != f"profile:{profile_id}"
            or payload.get("tool_id") != TOOL_ID
            or payload.get("arguments_sha256") != hashlib.sha256(encoded).hexdigest()
        ):
            raise ToolDenied("browser inspection call ID is already bound")
        job = jobs.by_idempotency_key(project_id, run_id, _call_key(project_id, run_id, call_id))
        if job is None:
            raise ToolOutcomeUnknown("browser inspection outcome is unknown; no job may be relaunched")
        if job["fixture_id"] != fixture_id or job["actor_id"] != f"profile:{profile_id}":
            raise ToolDenied("browser inspection call binding changed")
    else:
        try:
            job = jobs.launch(
                project_id,
                run_id,
                fixture_id,
                _call_key(project_id, run_id, call_id),
                role_profile_id=profile_id,
            )
        except (Conflict, NotFound, DBAPIError, OSError):
            raise ToolOutcomeUnknown("browser inspection launch outcome is unknown") from None
        result = _result(jobs, project_id, run_id, job)
        result_bytes = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
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
                        "status": result["status"],
                        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                        "result_bytes": len(result_bytes),
                    },
                )
        except DBAPIError:
            raise ToolOutcomeUnknown("browser inspection receipt could not be recorded") from None
    result = _result(jobs, project_id, run_id, job)
    result_bytes = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    if len(result_bytes) > settings.live_text_max_output_bytes:
        raise ToolOutputRejected("browser inspection result exceeds configured byte limit")
    return {
        "call_id": call_id,
        "tool_id": TOOL_ID,
        "version": 1,
        "status": result["status"],
        "result": result,
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
    }
