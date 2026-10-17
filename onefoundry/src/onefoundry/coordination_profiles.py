"""Chief of Staff agent proposals: an exact profile waits for operator approval.

``propose_profile_v1`` is available only to the built-in Chief of Staff. It saves
one proposal and one approval request whose content is a readable summary and
whose digest binds the exact proposal. Requested tools must sit inside the
operator's tool ceiling, both when proposed and when approved. Approval creates
the profile under the Chief and, optionally, one draft first work order, in the
same transaction as the decision.
"""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError

from onefoundry.gateway_errors import Conflict
from onefoundry.profile_hierarchy import chief_profile_id, lock_project, validate_parent
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

TOOL_ID = "propose_profile_v1"
NAME_LIMIT = 80
INSTRUCTION_LIMIT = 8000
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "maxLength": NAME_LIMIT},
        "instructions": {"type": "string", "maxLength": INSTRUCTION_LIMIT},
        "memory_scope": {"type": "string", "enum": ["company", "project"]},
        "allowed_tools": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
        "autonomy_level": {"type": "string", "enum": ["manual", "supervised"]},
        "use_chief_model": {"type": "boolean"},
        "first_work_order": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 160},
                "instructions": {"type": "string", "maxLength": 6000},
            },
            "required": ["title", "instructions"],
            "additionalProperties": False,
        },
        "reason": {"type": "string", "maxLength": 500},
    },
    "required": ["name", "instructions", "reason"],
    "additionalProperties": False,
}


def registered_tools(connection: Any = None, project_id: UUID | None = None) -> frozenset[str]:
    from onefoundry.role_document_tools import DESCRIPTORS
    from onefoundry.role_tool_admission import (
        LIVE_SDK_EXCLUDED_IDS,
        MEDIA_TOOL_IDS,
        READ_TOOLS,
        WRITE_TOOL_IDS,
    )
    from onefoundry.role_web_tools import WEB_TOOLS

    builtin = frozenset(
        set(READ_TOOLS)
        | WRITE_TOOL_IDS
        | MEDIA_TOOL_IDS
        | set(WEB_TOOLS)
        | set(DESCRIPTORS)
        | LIVE_SDK_EXCLUDED_IDS
    ) - {TOOL_ID}
    if connection is not None and project_id is not None:
        from onefoundry.gateway_extension_registry import registered_ids

        return builtin | registered_ids(connection, project_id)
    return builtin


def _safe(value: object, limit: int, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ToolInputError(f"{label} is invalid")
    try:
        cleaned = local_text(value)
    except ValueError:
        raise ToolInputError(f"{label} is invalid") from None
    if contains_sensitive_text(cleaned):
        raise ToolInputError(f"{label} is invalid")
    return cleaned


def parse_arguments(value: object) -> dict[str, Any]:
    allowed = {
        "name",
        "instructions",
        "memory_scope",
        "allowed_tools",
        "autonomy_level",
        "use_chief_model",
        "first_work_order",
        "reason",
    }
    if (
        not isinstance(value, dict)
        or not {"name", "instructions", "reason"} <= set(value)
        or set(value) - allowed
    ):
        raise ToolInputError("profile proposal arguments are invalid")
    tools = value.get("allowed_tools", [])
    if (
        not isinstance(tools, list)
        or len(tools) > 16
        or any(not isinstance(item, str) for item in tools)
        or len(set(tools)) != len(tools)
    ):
        raise ToolInputError("allowed tools are invalid")
    memory = value.get("memory_scope", "project")
    autonomy = value.get("autonomy_level", "manual")
    use_chief = value.get("use_chief_model", True)
    if (
        memory not in {"company", "project"}
        or autonomy not in {"manual", "supervised"}
        or not isinstance(use_chief, bool)
    ):
        raise ToolInputError("profile proposal settings are invalid")
    first = value.get("first_work_order")
    parsed_first: dict[str, str] | None = None
    if first is not None:
        if not isinstance(first, dict) or set(first) != {"title", "instructions"}:
            raise ToolInputError("first work order is invalid")
        parsed_first = {
            "title": _safe(first["title"], 160, "first work order title"),
            "instructions": _safe(first["instructions"], 6000, "first work order instructions"),
        }
    return {
        "name": _safe(value["name"], NAME_LIMIT, "name"),
        "instructions": _safe(value["instructions"], INSTRUCTION_LIMIT, "instructions"),
        "memory_scope": memory,
        "allowed_tools": sorted(tools),
        "autonomy_level": autonomy,
        "use_chief_model": use_chief,
        "first_work_order": parsed_first,
        "reason": _safe(value["reason"], 500, "reason"),
    }


def ceiling(connection: Any, project_id: UUID) -> list[str]:
    value = connection.scalar(
        text("SELECT profile_tool_ceiling FROM coordination_controls WHERE project_id=:p"), {"p": project_id}
    )
    return [str(item) for item in value] if value else []


def set_ceiling(
    gateway: ToolGateway, connection: Any, project_id: UUID, tools: list[str], expected: int
) -> None:
    unknown = sorted(set(tools) - registered_tools(connection, project_id))
    if unknown:
        raise Conflict(f"not a registered Tool Gateway ID: {', '.join(unknown)}")
    clean = sorted(set(tools))
    if expected == 0:
        changed = connection.execute(
            text(
                "INSERT INTO coordination_controls (project_id,actor_id,profile_tool_ceiling) "
                "VALUES (:p,:actor,:tools) ON CONFLICT (project_id) DO NOTHING"
            ).bindparams(bindparam("tools", type_=JSONB)),
            {"p": project_id, "actor": gateway.actor_id, "tools": clean},
        ).rowcount
    else:
        changed = connection.execute(
            text(
                "UPDATE coordination_controls SET profile_tool_ceiling=:tools,version=version+1,"
                "actor_id=:actor,"
                "updated_at=clock_timestamp() WHERE project_id=:p AND version=:v"
            ).bindparams(bindparam("tools", type_=JSONB)),
            {"p": project_id, "actor": gateway.actor_id, "tools": clean, "v": expected},
        ).rowcount
    if changed != 1:
        raise Conflict("coordination controls changed; reload before saving")
    gateway._event(connection, project_id, None, None, "coordination_tool_ceiling_saved", {"tools": clean})


def _summary(proposal: dict[str, Any]) -> str:
    tools = ", ".join(proposal["allowed_tools"]) or "none"
    lines = [
        f"Create agent “{proposal['name']}” reporting to the Chief of Staff.",
        f"Why: {proposal['reason']}",
        f"Tools: {tools}",
        f"Memory: {proposal['memory_scope']} · Autonomy: {proposal['autonomy_level']}",
        "Model: same as the Chief of Staff"
        if proposal["use_chief_model"]
        else "Model: none (assign one in Team)",
        "Instructions:",
        proposal["instructions"],
    ]
    if proposal["first_work_order"]:
        lines += [
            f"First work order (draft): {proposal['first_work_order']['title']}",
            proposal["first_work_order"]["instructions"],
        ]
    return "\n".join(lines)[:16000]


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
        raise ToolDenied("profile proposal tool version is not admitted")
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
                raise ToolOutcomeUnknown("profile proposal outcome is unknown; inspect the run timeline")
            if prior is not None:
                raise ToolDenied("profile proposal call already attempted; inspect the run timeline")
            if TOOL_ID not in context.snapshot_tools & context.current_tools:
                raise ToolDenied("profile proposal tool is not admitted for this run")
            if context.current_role_kind != "builtin" or context.current_role_name != "Chief of Staff":
                raise ToolDenied("only the Chief of Staff can propose agents")
            parsed, encoded = _argument_bytes(arguments, settings.live_text_max_prompt_bytes, parse_arguments)
            outside = sorted(set(parsed["allowed_tools"]) - set(ceiling(connection, project_id)))
            if outside:
                raise ToolDenied(f"requested tools are outside the operator's ceiling: {', '.join(outside)}")
            digest = hashlib.sha256(json.dumps(parsed, sort_keys=True).encode()).hexdigest()
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
            approval_id, proposal_id = uuid4(), uuid4()
            actor = f"profile:{profile_id}"
            connection.execute(
                text(
                    "INSERT INTO approval_requests (id,project_id,actor_id,work_order_id,run_id,action_type,"
                    "target,content,proposal_digest,status) VALUES (:id,:p,:actor,:o,:r,'other',:target,"
                    ":content,"
                    ":digest,'pending')"
                ),
                {
                    "id": approval_id,
                    "p": project_id,
                    "actor": actor,
                    "o": context.work_order_id,
                    "r": run_id,
                    "target": f"New agent: {parsed['name']}"[:500],
                    "content": _summary(parsed),
                    "digest": digest,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO coordination_profile_proposals (id,project_id,actor_id,work_order_id,run_id,"
                    "approval_id,proposal,proposal_sha256) VALUES (:id,:p,:actor,:o,:r,:a,:proposal,:digest)"
                ).bindparams(bindparam("proposal", type_=JSONB)),
                {
                    "id": proposal_id,
                    "p": project_id,
                    "actor": actor,
                    "o": context.work_order_id,
                    "r": run_id,
                    "a": approval_id,
                    "proposal": parsed,
                    "digest": digest,
                },
            )
            result = {"approval_id": str(approval_id), "proposal_id": str(proposal_id), "status": "pending"}
            result_bytes = json.dumps(result, separators=(",", ":")).encode()
            _event(
                gateway,
                connection,
                context,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": TOOL_ID,
                    "status": "done",
                    "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                    "result_bytes": len(result_bytes),
                },
            )
        return {
            "call_id": call_id,
            "tool_id": TOOL_ID,
            "version": 1,
            "status": "done",
            "result": result,
            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        }
    except DBAPIError:
        raise ToolOutcomeUnknown(
            "profile proposal transaction outcome is unknown; inspect the run timeline"
        ) from None


def lock_decision_project(connection: Any, project_id: UUID, approval_id: UUID) -> None:
    """Lock the project before an approved profile proposal's work order."""
    bound = connection.scalar(
        text(
            "SELECT 1 FROM coordination_profile_proposals "
            "WHERE project_id=:p AND approval_id=:a AND state='pending'"
        ),
        {"p": project_id, "a": approval_id},
    )
    if bound:
        lock_project(connection, project_id)


def apply_decision(
    gateway: ToolGateway, connection: Any, project_id: UUID, approval: dict[str, Any], decision: str
) -> None:
    """Called inside the approval transaction. Approval creates exactly the proposed agent."""
    proposal = (
        connection.execute(
            text(
                "SELECT id,proposal,proposal_sha256,work_order_id FROM coordination_profile_proposals "
                "WHERE project_id=:p AND approval_id=:a AND state='pending' FOR UPDATE"
            ),
            {"p": project_id, "a": approval["id"]},
        )
        .mappings()
        .one_or_none()
    )
    if proposal is None:
        return
    data = dict(proposal["proposal"])
    if hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest() != approval["proposal_digest"]:
        raise Conflict("agent proposal no longer matches its approval request")
    if decision != "approved":
        connection.execute(
            text(
                "UPDATE coordination_profile_proposals SET state='rejected',decided_at=clock_timestamp() "
                "WHERE project_id=:p AND id=:id"
            ),
            {"p": project_id, "id": proposal["id"]},
        )
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "agent_proposal_rejected",
            {"proposal_id": str(proposal["id"])},
        )
        return
    outside = sorted(set(data["allowed_tools"]) - set(ceiling(connection, project_id)))
    if outside:
        raise Conflict(
            f"requested tools are now outside the tool ceiling: {', '.join(outside)}; reject instead"
        )
    lock_project(connection, project_id)
    chief = chief_profile_id(connection, project_id)
    model, account, effort = None, None, None
    if data["use_chief_model"]:
        chief_row = (
            connection.execute(
                text(
                    "SELECT model,model_account_id,reasoning_effort FROM profiles "
                    "WHERE project_id=:p AND id=:id"
                ),
                {"p": project_id, "id": chief},
            )
            .mappings()
            .one()
        )
        model, account, effort = (
            chief_row["model"],
            chief_row["model_account_id"],
            chief_row["reasoning_effort"],
        )
    if connection.scalar(
        text("SELECT 1 FROM profiles WHERE project_id=:p AND lower(name)=lower(:n)"),
        {"p": project_id, "n": data["name"]},
    ):
        raise Conflict("an agent with this name already exists; reject the proposal instead")
    profile_id = uuid4()
    validate_parent(connection, project_id, profile_id, chief)
    connection.execute(
        text(
            "INSERT INTO profiles (id,project_id,actor_id,name,kind,model,model_account_id,reasoning_effort,"
            "instructions,memory_scope,allowed_tools,autonomy_level,configured,reports_to_profile_id) VALUES "
            "(:id,:p,:actor,:name,'custom',:model,:account,:effort,:instructions,:memory,:tools,:autonomy,"
            ":configured,:chief)"
        ).bindparams(bindparam("tools", type_=JSONB)),
        {
            "id": profile_id,
            "p": project_id,
            "actor": gateway.actor_id,
            "name": data["name"],
            "model": model,
            "account": account,
            "effort": effort,
            "instructions": data["instructions"],
            "memory": data["memory_scope"],
            "tools": data["allowed_tools"],
            "autonomy": data["autonomy_level"],
            "configured": bool(model and data["instructions"]),
            "chief": chief,
        },
    )
    first_id: UUID | None = None
    if data["first_work_order"]:
        first_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO work_orders (id,project_id,actor_id,title,instructions,profile_id,"
                "parent_work_order_id,"
                "status) VALUES (:id,:p,:actor,:title,:instructions,:profile,:parent,'draft')"
            ),
            {
                "id": first_id,
                "p": project_id,
                "actor": gateway.actor_id,
                "title": data["first_work_order"]["title"],
                "instructions": data["first_work_order"]["instructions"],
                "profile": profile_id,
                "parent": proposal["work_order_id"],
            },
        )
        gateway._event(
            connection,
            project_id,
            first_id,
            None,
            "work_order_created",
            {"status": "draft", "profile_id": str(profile_id), "source": "agent_proposal"},
        )
    connection.execute(
        text(
            "UPDATE coordination_profile_proposals SET state='approved',profile_id=:profile,"
            "first_work_order_id=:first,decided_at=clock_timestamp() WHERE project_id=:p AND id=:id"
        ),
        {"profile": profile_id, "first": first_id, "p": project_id, "id": proposal["id"]},
    )
    gateway._event(
        connection,
        project_id,
        None,
        None,
        "agent_proposal_approved",
        {"proposal_id": str(proposal["id"]), "profile_id": str(profile_id), "tools": data["allowed_tools"]},
    )
