"""Durable role admission around document reads and local approval proposals."""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError

from onefoundry import document_action_store, document_operations
from onefoundry.document_action_schemas import ActionInput, RangeRead, Search, parse_action
from onefoundry.document_api_sources import DocumentError
from onefoundry.gateway_errors import Conflict, Unavailable
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.role_tool_admission import (
    CALL_ID,
    ToolDenied,
    ToolDependencyFailure,
    ToolInputError,
    ToolOutcomeUnknown,
    ToolOutputRejected,
    _argument_bytes,
    _context,
    _db_bounds,
    _event,
    _prior_attempt,
    _reject_sensitive_output,
)

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

DESCRIPTORS: dict[str, dict[str, Any]] = {
    "document_search_v1": {
        "description": "Find files by name in one ready selected folder's direct children. "
        "Returns bounded metadata, never searches other folders.",
        "parameters": Search.model_json_schema(),
    },
    "document_range_v1": {
        "description": "Read one bounded literal A1 rectangle from an Excel workbook or Google Sheet "
        "directly in a selected folder.",
        "parameters": RangeRead.model_json_schema(),
    },
    "propose_document_action_v1": {
        "description": "Propose one exact new version from a project file, cell edit, or recipient sharing "
        "permission. No external write until operator approval and dispatch.",
        "parameters": ActionInput.model_json_schema(),
    },
}


def _parse(tool_id: str, raw: object) -> dict[str, Any]:
    try:
        model = (
            Search.model_validate(raw)
            if tool_id == "document_search_v1"
            else RangeRead.model_validate(raw)
            if tool_id == "document_range_v1"
            else parse_action(raw)
        )
    except ValidationError:
        raise ToolInputError("document arguments do not match the registered contract") from None
    result = model.model_dump(mode="json")
    _reject_sensitive_output(result)
    return result


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    tool_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None or contains_sensitive_text(call_id):
        raise ToolInputError("tool call identifier is invalid")
    settings, store = gateway.live_text_settings, gateway.document_store
    if settings is None or store is None or type(tool_version) is not int or tool_version != 1:
        raise ToolDenied("document tools are unavailable for this run")
    deadline = monotonic() + settings.live_text_timeout_seconds
    with gateway.engine.begin() as connection:
        _db_bounds(connection, deadline)
        context = _context(
            connection, project_id, run_id, profile_id, lock=True, max_items=settings.role_context_max_items
        )
        prior = _prior_attempt(connection, project_id, run_id, call_id)
        if prior == "role_tool_started":
            raise ToolOutcomeUnknown("document tool outcome is unknown; inspect the run timeline")
        if prior is not None or tool_id not in context.current_tools & context.snapshot_tools:
            raise ToolDenied("document tool is not admitted or this call was already attempted")
        parsed, encoded = _argument_bytes(
            arguments, settings.live_text_max_prompt_bytes, lambda raw: _parse(tool_id, raw)
        )
        _event(
            gateway,
            connection,
            context,
            "role_tool_started",
            {
                "call_id": call_id,
                "tool_id": tool_id,
                "version": 1,
                "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
                "argument_bytes": len(encoded),
            },
        )
    try:
        if tool_id == "document_search_v1":
            result = document_operations.search(store, project_id, Search.model_validate(parsed))
        elif tool_id == "document_range_v1":
            result = document_operations.read_range(store, project_id, RangeRead.model_validate(parsed))
        else:
            result = document_operations.prepare(store, project_id, context.work_order_id, parsed)
        _reject_sensitive_output(result)
        encoded_result = json.dumps(result, allow_nan=False, separators=(",", ":")).encode()
        if len(encoded_result) > settings.live_text_max_prompt_bytes:
            raise ToolOutputRejected("document result exceeds the configured context byte limit")
    except (DocumentError, Conflict, Unavailable, ToolOutputRejected) as error:
        with gateway.engine.begin() as connection:
            _event(
                gateway,
                connection,
                context,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": tool_id,
                    "status": "failed",
                    "reason": error.code if isinstance(error, DocumentError) else "document_unavailable",
                },
            )
        raise ToolDependencyFailure("document operation failed; inspect the recorded result") from None
    with gateway.engine.begin() as connection:
        _db_bounds(connection, deadline)
        current = _context(
            connection, project_id, run_id, profile_id, lock=True, max_items=settings.role_context_max_items
        )
        if tool_id not in current.current_tools & current.snapshot_tools:
            raise ToolDenied("document tool permission changed while reading")
        if tool_id == "propose_document_action_v1":
            result = document_action_store.save(gateway, connection, current, result)
            encoded_result = json.dumps(result, separators=(",", ":")).encode()
        checksum = hashlib.sha256(encoded_result).hexdigest()
        _event(
            gateway,
            connection,
            current,
            "role_tool_finished",
            {
                "call_id": call_id,
                "tool_id": tool_id,
                "status": "done",
                "result_sha256": checksum,
                "result_bytes": len(encoded_result),
            },
        )
    return {
        "call_id": call_id,
        "tool_id": tool_id,
        "version": 1,
        "status": "done",
        "result": result,
        "result_sha256": checksum,
    }
