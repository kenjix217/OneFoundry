"""Request project-local QA review from an admitted source role run."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

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
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


TOOL_ID = "request_qa_review_v1"
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_profile_id": {"type": "string", "format": "uuid"},
        "note": {"type": "string", "maxLength": 2000},
    },
    "required": ["target_profile_id", "note"],
    "additionalProperties": False,
}


class _Arguments(StrictModel):
    target_profile_id: str
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def safe_note(cls, value: str) -> str:
        value = local_text(value)
        if any(unicodedata.category(char).startswith("C") and char not in "\n\t" for char in value):
            raise ValueError("note contains unsafe control characters")
        return value


def parse_arguments(value: object) -> dict[str, str]:
    try:
        parsed = _Arguments.model_validate(value, strict=True)
        target = UUID(parsed.target_profile_id)
        if target.int == 0 or str(target) != parsed.target_profile_id:
            raise ValueError("target profile ID is not canonical")
        if contains_sensitive_text(parsed.note):
            raise ValueError("sensitive note")
    except (ValueError, ValidationError):
        raise ToolInputError("QA review request arguments are invalid") from None
    return {"target_profile_id": str(target), "note": parsed.note}


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    """Commit one local QA request and its hash-only tool receipt together."""
    if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None or contains_sensitive_text(call_id):
        raise ToolInputError("tool call identifier is invalid")
    if type(tool_version) is not int or tool_version != 1:
        raise ToolDenied("QA review request tool version is not admitted")
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
                raise ToolOutcomeUnknown("QA review request outcome is unknown; inspect the run timeline")
            if prior is not None:
                raise ToolDenied("QA review request call already attempted; inspect the run timeline")
            if TOOL_ID not in context.snapshot_tools & context.current_tools:
                raise ToolDenied("QA review request tool is not admitted for this run")
            parsed, encoded = _argument_bytes(arguments, settings.live_text_max_prompt_bytes, parse_arguments)
            target_profile_id = UUID(parsed["target_profile_id"])
            target = (
                connection.execute(
                    text(
                        "SELECT name,kind FROM profiles WHERE project_id=:project_id AND id=:profile_id "
                        "FOR KEY SHARE"
                    ),
                    {"project_id": project_id, "profile_id": target_profile_id},
                )
                .mappings()
                .one_or_none()
            )
            if target is None or target["name"] != "QA" or target["kind"] != "builtin":
                raise ToolDenied("target must be this project's built-in QA profile")
            note_sha256 = hashlib.sha256(parsed["note"].encode("utf-8")).hexdigest()
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
                "qa_review_requested",
                {
                    "call_id": call_id,
                    "target_profile_id": str(target_profile_id),
                    "note": parsed["note"],
                    "note_sha256": note_sha256,
                },
            )
            saved = (
                connection.execute(
                    text(
                        "SELECT payload->>'note' AS note,payload->>'target_profile_id' AS target_profile_id "
                        "FROM events WHERE project_id=:project_id AND work_order_id=:work_order_id "
                        "AND run_id=:run_id AND event_type='qa_review_requested' "
                        "AND payload->>'call_id'=:call_id ORDER BY created_at DESC,id DESC LIMIT 1"
                    ),
                    {
                        "project_id": project_id,
                        "work_order_id": context.work_order_id,
                        "run_id": run_id,
                        "call_id": call_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if (
                saved is None
                or saved["note"] != parsed["note"]
                or saved["target_profile_id"] != str(target_profile_id)
            ):
                raise ToolOutcomeUnknown("QA review request owner readback did not match")
            result = {
                "status": "proposed",
                "target_profile_id": str(target_profile_id),
                "note_sha256": note_sha256,
            }
            result_bytes = json.dumps(result, separators=(",", ":")).encode("utf-8")
            result_sha256 = hashlib.sha256(result_bytes).hexdigest()
            _event(
                gateway,
                connection,
                context,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "status": "done",
                    "result_sha256": result_sha256,
                    "result_bytes": len(result_bytes),
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
    except DBAPIError:
        raise ToolOutcomeUnknown(
            "QA review request transaction outcome is unknown; inspect the run timeline"
        ) from None
