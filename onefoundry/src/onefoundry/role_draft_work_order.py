"""Create one project-local draft from an admitted, active role run."""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
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
from onefoundry.workflow_schemas import WorkOrderCreate

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


TOOL_ID = "create_draft_work_order_v1"
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target_profile_id": {"type": "string", "format": "uuid"},
        "title": {"type": "string", "maxLength": 160},
        "instructions": {"type": "string", "maxLength": 16000},
        "deliverable_kind": {
            "type": "string",
            "enum": [
                "text_reply",
                "artifact",
                "document",
                "code_change",
                "external_action",
                "operator_review",
            ],
        },
        "acceptance_checks": {"type": "array", "items": {"type": "string", "maxLength": 300}, "maxItems": 8},
    },
    "required": ["target_profile_id", "title", "instructions"],
    "additionalProperties": False,
}


def parse_arguments(value: object) -> dict[str, Any]:
    required = {"target_profile_id", "title", "instructions"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - {"deliverable_kind", "acceptance_checks"}
    ):
        raise ToolInputError("draft work-order arguments are invalid")
    profile_text = value["target_profile_id"]
    if (
        not isinstance(profile_text, str)
        or not isinstance(value["title"], str)
        or not isinstance(value["instructions"], str)
    ):
        raise ToolInputError("draft work-order arguments are invalid")
    try:
        profile_id = UUID(profile_text)
        if profile_id.int == 0 or str(profile_id) != profile_text:
            raise ValueError("profile ID is not canonical")
        data = WorkOrderCreate.model_validate(
            {
                "profile_id": profile_id,
                "title": value["title"],
                "instructions": value["instructions"],
                "deliverable_kind": value.get("deliverable_kind", "operator_review"),
                "acceptance_checks": value.get("acceptance_checks", []),
            }
        )
        if contains_sensitive_text(data.title) or contains_sensitive_text(data.instructions):
            raise ValueError("sensitive draft content")
    except (ValueError, ValidationError):
        raise ToolInputError("draft work-order arguments are invalid") from None
    return {
        "target_profile_id": str(data.profile_id),
        "title": data.title,
        "instructions": data.instructions,
        "deliverable_kind": data.deliverable_kind,
        "acceptance_checks": data.acceptance_checks,
    }


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    """Atomically save call intent, one draft, and a hash-only result receipt."""
    if not isinstance(call_id, str) or CALL_ID.fullmatch(call_id) is None or contains_sensitive_text(call_id):
        raise ToolInputError("tool call identifier is invalid")
    if type(tool_version) is not int or tool_version != 1:
        raise ToolDenied("draft work-order tool version is not admitted")
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
                raise ToolOutcomeUnknown("draft work-order call outcome is unknown; inspect the run timeline")
            if prior is not None:
                raise ToolDenied("draft work-order call already attempted; inspect the run timeline")
            if TOOL_ID not in context.snapshot_tools & context.current_tools:
                raise ToolDenied("draft work-order tool is not admitted for this run")
            parsed, encoded = _argument_bytes(arguments, settings.live_text_max_prompt_bytes, parse_arguments)
            target_profile_id = UUID(parsed["target_profile_id"])
            if (
                connection.scalar(
                    text("SELECT 1 FROM profiles WHERE project_id=:project_id AND id=:id FOR KEY SHARE"),
                    {"project_id": project_id, "id": target_profile_id},
                )
                is None
            ):
                raise ToolDenied("target profile is not in this project")
            digest = hashlib.sha256(encoded).hexdigest()
            order_id = uuid4()
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
            row = (
                connection.execute(
                    text(
                        "INSERT INTO work_orders "
                        "(id,project_id,actor_id,title,instructions,profile_id,deliverable_kind,"
                        "acceptance_checks,parent_work_order_id,source_run_id,status) "
                        "VALUES (:id,:project_id,:actor_id,:title,:instructions,:profile_id,:kind,"
                        ":checks,:parent,:source,'draft') "
                        "RETURNING id,profile_id,status"
                    ).bindparams(bindparam("checks", type_=JSONB)),
                    {
                        "id": order_id,
                        "project_id": project_id,
                        "actor_id": f"profile:{profile_id}",
                        "title": parsed["title"],
                        "instructions": parsed["instructions"],
                        "profile_id": target_profile_id,
                        "kind": parsed["deliverable_kind"],
                        "checks": parsed["acceptance_checks"],
                        "parent": context.work_order_id,
                        "source": run_id,
                    },
                )
                .mappings()
                .one()
            )
            if row["id"] != order_id or row["profile_id"] != target_profile_id or row["status"] != "draft":
                raise ToolOutcomeUnknown("draft work-order owner readback did not match")
            result = {
                "work_order_id": str(order_id),
                "profile_id": str(target_profile_id),
                "status": "draft",
                "parent_work_order_id": str(context.work_order_id),
            }
            result_bytes = json.dumps(result, separators=(",", ":")).encode("utf-8")
            result_sha256 = hashlib.sha256(result_bytes).hexdigest()
            gateway._event(
                connection,
                project_id,
                order_id,
                None,
                "work_order_created",
                {
                    "status": "draft",
                    "profile_id": str(target_profile_id),
                    "source_run_id": str(run_id),
                    "parent_work_order_id": str(context.work_order_id),
                    "arguments_sha256": digest,
                },
                actor_id=f"profile:{profile_id}",
            )
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
            "draft work-order transaction outcome is unknown; inspect the run timeline"
        ) from None
