"""Admitted agent tool that saves a pending project-local skill draft."""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

from onefoundry.gateway_extension_schemas import DraftCreate
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

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

TOOL_ID = "propose_extension_skill_v1"
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "maxLength": 120},
        "description": {"type": "string", "maxLength": 500},
        "content": {"type": "string", "maxLength": 16000},
        "required_tools": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
        "package_id": {"type": "string", "format": "uuid"},
        "target_skill_id": {"type": "string", "format": "uuid"},
        "expected_base_version": {"type": "integer", "minimum": 1},
    },
    "required": ["name", "content"],
    "additionalProperties": False,
}


def parse_arguments(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"name", "content"} <= set(value):
        raise ToolInputError("skill proposal arguments are invalid")
    if set(value) - {
        "name",
        "description",
        "content",
        "required_tools",
        "package_id",
        "target_skill_id",
        "expected_base_version",
    }:
        raise ToolInputError("skill proposal arguments are invalid")
    if any(not isinstance(value.get(key), str) for key in ("name", "content")):
        raise ToolInputError("skill proposal arguments are invalid")
    for field in ("package_id", "target_skill_id"):
        identifier = value.get(field)
        if identifier is None:
            continue
        try:
            if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
                raise ValueError("identifier is not canonical")
        except ValueError:
            raise ToolInputError("skill proposal identifier is invalid") from None
    try:
        data = DraftCreate.model_validate({**value, "kind": "skill"})
    except (ValueError, ValidationError):
        raise ToolInputError("skill proposal arguments are invalid") from None
    if any(contains_sensitive_text(part) for part in (data.name, data.description, data.content)):
        raise ToolInputError("skill proposal contains sensitive text")
    return data.model_dump(mode="json", exclude={"kind", "source_run_id"})


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None or contains_sensitive_text(call_id):
        raise ToolInputError("tool call identifier is invalid")
    if type(tool_version) is not int or tool_version != 1:
        raise ToolDenied("skill proposal tool version is not admitted")
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
                raise ToolOutcomeUnknown("skill proposal outcome is unknown; inspect the run timeline")
            if prior is not None:
                raise ToolDenied("skill proposal call already attempted; inspect the run timeline")
            if TOOL_ID not in context.snapshot_tools & context.current_tools:
                raise ToolDenied("skill proposal tool is not admitted for this run")
            parsed, encoded = _argument_bytes(arguments, settings.live_text_max_prompt_bytes, parse_arguments)
            digest = hashlib.sha256(encoded).hexdigest()
            _event(
                gateway,
                connection,
                context,
                "role_tool_started",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "version": 1,
                    "arguments_sha256": digest,
                    "argument_bytes": len(encoded),
                },
            )
            draft = gateway._extensions().create_draft_in_transaction(
                connection,
                project_id,
                DraftCreate.model_validate({**parsed, "source_run_id": run_id}),
                actor_id=f"profile:{profile_id}",
            )
            result = {"draft_id": str(draft["id"]), "status": draft["status"], "sha256": draft["sha256"]}
            result_bytes = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
            result_digest = hashlib.sha256(result_bytes).hexdigest()
            _event(
                gateway,
                connection,
                context,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "status": "done",
                    "result_sha256": result_digest,
                    "result_bytes": len(result_bytes),
                },
            )
        return {
            "call_id": call_id,
            "tool_id": TOOL_ID,
            "version": 1,
            "status": "done",
            "result": result,
            "result_sha256": result_digest,
        }
    except DBAPIError:
        raise ToolOutcomeUnknown("skill proposal outcome is unknown; inspect the run timeline") from None
