"""Let an admitted role run report one finding as a coordination signal.

The finding never reaches another agent directly. It becomes a signal that
operator rules or Chief of Staff triage handle under the same limits.
"""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from onefoundry import coordination_store
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
from onefoundry.workflow_schemas import local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

TOOL_ID = "report_signal_v1"
TOPICS = ("lead", "opportunity", "risk", "deadline", "bug", "customer", "other")
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "enum": list(TOPICS)},
        "summary": {"type": "string", "maxLength": 500},
    },
    "required": ["topic", "summary"],
    "additionalProperties": False,
}


def parse_arguments(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"topic", "summary"}:
        raise ToolInputError("finding arguments are invalid")
    topic, summary = value["topic"], value["summary"]
    if topic not in TOPICS or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 500:
        raise ToolInputError("finding arguments are invalid")
    try:
        summary = local_text(summary)
    except ValueError:
        raise ToolInputError("finding arguments are invalid") from None
    if contains_sensitive_text(summary):
        raise ToolInputError("finding arguments are invalid")
    return {"topic": topic, "summary": summary}


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    """Atomically save the call intent, one signal, and a hash-only receipt."""
    if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None or contains_sensitive_text(call_id):
        raise ToolInputError("tool call identifier is invalid")
    if type(tool_version) is not int or tool_version != 1:
        raise ToolDenied("finding tool version is not admitted")
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    if not coordination_store.recording_enabled(gateway):
        raise ToolDenied("automation is off; findings are not recorded")
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
                raise ToolOutcomeUnknown("finding call outcome is unknown; inspect the run timeline")
            if prior is not None:
                raise ToolDenied("finding call already attempted; inspect the run timeline")
            if TOOL_ID not in context.snapshot_tools & context.current_tools:
                raise ToolDenied("finding tool is not admitted for this run")
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
            depth = connection.scalar(
                text("SELECT chain_depth FROM work_orders WHERE project_id=:p AND id=:o"),
                {"p": project_id, "o": context.work_order_id},
            )
            signal_id = coordination_store.record_signal(
                gateway,
                connection,
                project_id,
                source="agent_finding",
                signal_type="agent.finding",
                dedupe_key=f"finding:{run_id}:{call_id}",
                fields={
                    "topic": parsed["topic"],
                    "summary": parsed["summary"],
                    "profile_id": str(profile_id),
                    "profile_name": context.current_role_name,
                    "work_order_id": str(context.work_order_id),
                },
                origin_work_order_id=context.work_order_id,
                origin_run_id=run_id,
                chain_depth=int(depth or 0),
            )
            if signal_id is None:
                raise ToolOutcomeUnknown("finding was already recorded; inspect the run timeline")
            result = {"signal_id": str(signal_id), "topic": parsed["topic"], "status": "recorded"}
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
        raise ToolOutcomeUnknown("finding transaction outcome is unknown; inspect the run timeline") from None
