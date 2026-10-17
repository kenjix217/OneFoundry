"""Default-deny admission for OneFoundry-owned, read-only role tools.

The provider runner is the only intended caller. No HTTP route accepts a claimed
agent identity, and a procedure or profile tool ID alone cannot invoke a tool.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from onefoundry import (
    role_coding_artifact,
    role_coding_evidence,
    role_context_reads,
    role_decimal_calculate,
    role_repository_file,
    role_repository_range,
    role_repository_tree,
    role_web_tools,
)
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.role_sensitive_text import contains_sensitive_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TOOL_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SENSITIVE_KEYS = (
    "apikey",
    "accesskey",
    "password",
    "passwd",
    "secret",
    "credential",
    "token",
    "sessionkey",
    "accesstoken",
    "refreshtoken",
    "privatekey",
    "authorization",
)

# Raw source/diff bytes need exact operator declassification before they may
# enter a provider context. PR reads also need a live approval wait/resume.
LIVE_SDK_EXCLUDED_IDS = frozenset(
    {
        "repository_file_v1",
        "repository_tree_v1",
        "repository_file_range_v1",
        "coding_artifact_text_v1",
        "github_pr_read_v1",
        "github_pr_comment_v1",
        "browser_static_inspect_v1",
    }
)
MEDIA_TOOL_IDS = frozenset({"edit_video_v1", "video_edit_status_v1", "cancel_video_edit_v1"})

WRITE_TOOL_IDS = frozenset(
    {
        "write_text_artifact_v1",
        "request_video_capture_v1",
        "edit_video_v1",
        "cancel_video_edit_v1",
        "create_draft_work_order_v1",
        "request_qa_review_v1",
        "report_signal_v1",
        "propose_profile_v1",
        "propose_document_write_v1",
        "propose_document_action_v1",
        "propose_email_v1",
        "propose_zoho_mail_v1",
        "propose_extension_skill_v1",
    }
)


class ToolInputError(ValueError):
    """The requested tool or arguments do not match a registered contract."""


class ToolDenied(Conflict):
    """A registered role tool is unavailable to this exact run and profile."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class ToolOutcomeUnknown(Exception):
    """A tool started, but its durable outcome could not be confirmed."""


class ToolDependencyFailure(Exception):
    """The admitted tool's owning dependency failed."""


class ToolOutputRejected(Exception):
    """The tool returned data that cannot safely enter the model context."""


class ToolPolicyRejected(Exception):
    """A bounded web request violated the public-source policy."""


class ToolTimedOut(Exception):
    """The admitted tool exceeded its configured time bound."""


@dataclass(frozen=True, slots=True)
class RunContext:
    project_id: UUID
    run_id: UUID
    work_order_id: UUID
    profile_id: UUID
    snapshot_tools: frozenset[str]
    current_tools: frozenset[str]
    snapshot_memory_scope: str
    current_memory_scope: str
    max_items: int
    current_role_name: str
    current_role_kind: str
    snapshot_role_name: str


@dataclass(frozen=True, slots=True)
class ReadTool:
    # Run snapshots pin IDs; publish changed semantics under a new ID.
    tool_id: str
    version: int
    description: str
    parameters: dict[str, Any]
    parse_arguments: Callable[[object], dict[str, Any]]
    execute: Callable[[Connection, RunContext, dict[str, Any]], dict[str, Any]]
    validate_output: Callable[[dict[str, Any]], None]
    stable_snapshot: bool = False
    execute_with_gateway: (
        Callable[[ToolGateway, Connection, RunContext, dict[str, Any]], dict[str, Any]] | None
    ) = None


def _read_hybrid_memory(
    gateway: ToolGateway, connection: Connection, context: RunContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    return role_context_reads.approved_memory(connection, context, arguments, gateway.live_text_settings)


def _read_repository_file(
    gateway: ToolGateway, connection: Connection, context: RunContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    return role_repository_file.repository_file(connection, context, arguments, settings)


def _read_repository_tree(
    gateway: ToolGateway, connection: Connection, context: RunContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    return role_repository_tree.repository_tree(connection, context, arguments, settings)


def _read_repository_range(
    gateway: ToolGateway, connection: Connection, context: RunContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    return role_repository_range.repository_range(connection, context, arguments, settings)


def _gateway_only(
    _connection: Connection, _context: RunContext, _arguments: dict[str, Any]
) -> dict[str, Any]:
    raise ToolDenied("this read requires the Tool Gateway owner")


def _calculate_decimal(
    _connection: Connection, _context: RunContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    return role_decimal_calculate.calculate_decimal(arguments).model_dump()


def _read_coding_artifact(
    gateway: ToolGateway, connection: Connection, context: RunContext, arguments: dict[str, Any]
) -> dict[str, Any]:
    return role_coding_artifact.coding_artifact(
        connection, context, arguments, gateway._execution_gateway().spool
    )


def _parse_empty_arguments(arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments:
        raise ValueError("tool arguments must be empty")
    return {}


def _current_work_order(
    connection: Connection, context: RunContext, _arguments: dict[str, Any]
) -> dict[str, Any]:
    row = (
        connection.execute(
            text("SELECT title,status FROM work_orders WHERE project_id=:project_id AND id=:order_id"),
            {"project_id": context.project_id, "order_id": context.work_order_id},
        )
        .mappings()
        .one()
    )
    return {
        "work_order_id": str(context.work_order_id),
        "title": row["title"],
        "status": row["status"],
    }


def _validate_current_work_order(result: dict[str, Any]) -> None:
    if set(result) != {"work_order_id", "title", "status"}:
        raise ToolOutputRejected("work order tool returned unexpected fields")
    if (
        not isinstance(result["work_order_id"], str)
        or not isinstance(result["title"], str)
        or result["status"] not in {"draft", "ready", "running", "blocked", "done", "failed", "unknown"}
    ):
        raise ToolOutputRejected("work order tool returned invalid fields")


READ_TOOLS: dict[str, ReadTool] = {
    "decimal_calculate_v1": ReadTool(
        tool_id="decimal_calculate_v1",
        version=1,
        description="Calculate bounded decimal arithmetic with exact string operands.",
        parameters=role_decimal_calculate.PARAMETERS,
        parse_arguments=role_decimal_calculate.parse_calculation,
        execute=_calculate_decimal,
        validate_output=role_decimal_calculate.validate_calculation,
    ),
    "current_work_order": ReadTool(
        tool_id="current_work_order",
        version=1,
        description="Read the title and status of this run's work order.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        parse_arguments=_parse_empty_arguments,
        execute=_current_work_order,
        validate_output=_validate_current_work_order,
    ),
    "project_brief_v1": ReadTool(
        tool_id="project_brief_v1",
        version=1,
        description="Read the active project's name, goal, and desired outcome.",
        parameters=role_context_reads.EMPTY_PARAMETERS,
        parse_arguments=role_context_reads.parse_empty,
        execute=role_context_reads.project_brief,
        validate_output=role_context_reads.validate_project_brief,
    ),
    "recent_project_work_v1": ReadTool(
        tool_id="recent_project_work_v1",
        version=1,
        description="Read bounded recent work-order and run status in this project.",
        parameters=role_context_reads.EMPTY_PARAMETERS,
        parse_arguments=role_context_reads.parse_empty,
        execute=role_context_reads.recent_project_work,
        validate_output=role_context_reads.validate_recent_project_work,
    ),
    "run_evidence_v1": ReadTool(
        tool_id="run_evidence_v1",
        version=1,
        description="Read one project run's result, artifact metadata, and event types.",
        parameters=role_context_reads.RUN_PARAMETERS,
        parse_arguments=role_context_reads.parse_run,
        execute=role_context_reads.run_evidence,
        validate_output=role_context_reads.validate_run_evidence,
    ),
    "coding_evidence_v1": ReadTool(
        tool_id="coding_evidence_v1",
        version=1,
        description=(
            "Read bounded coding attempts for one project run. Status is an owner record; "
            "only verified_outcome confirms a terminal coding receipt."
        ),
        parameters=role_coding_evidence.CODING_EVIDENCE_PARAMETERS,
        parse_arguments=role_coding_evidence.parse_coding_evidence,
        execute=role_coding_evidence.coding_evidence,
        validate_output=role_coding_evidence.validate_coding_evidence,
        stable_snapshot=True,
    ),
    "repository_file_v1": ReadTool(
        tool_id="repository_file_v1",
        version=1,
        description="Read bounded UTF-8 text from an exact saved local or GitHub repository revision.",
        parameters=role_repository_file.REPOSITORY_FILE_PARAMETERS,
        parse_arguments=role_repository_file.parse_repository_file,
        execute=_gateway_only,
        execute_with_gateway=_read_repository_file,
        validate_output=role_repository_file.validate_repository_file,
        stable_snapshot=True,
    ),
    "repository_tree_v1": ReadTool(
        tool_id="repository_tree_v1",
        version=1,
        description="List a bounded page of safe entries at an exact saved repository commit.",
        parameters=role_repository_tree.TREE_PARAMETERS,
        parse_arguments=role_repository_tree.parse_repository_tree,
        execute=_gateway_only,
        execute_with_gateway=_read_repository_tree,
        validate_output=role_repository_tree.validate_repository_tree,
        stable_snapshot=True,
    ),
    "repository_file_range_v1": ReadTool(
        tool_id="repository_file_range_v1",
        version=1,
        description="Page a fully screened exact committed UTF-8 document by character offset.",
        parameters=role_repository_range.RANGE_PARAMETERS,
        parse_arguments=role_repository_range.parse_repository_range,
        execute=_gateway_only,
        execute_with_gateway=_read_repository_range,
        validate_output=role_repository_range.validate_repository_range,
        stable_snapshot=True,
    ),
    "coding_artifact_text_v1": ReadTool(
        tool_id="coding_artifact_text_v1",
        version=1,
        description="Read bounded verified diff or log text from a saved coding attempt.",
        parameters=role_coding_artifact.CODING_ARTIFACT_PARAMETERS,
        parse_arguments=role_coding_artifact.parse_coding_artifact,
        execute=_gateway_only,
        execute_with_gateway=_read_coding_artifact,
        validate_output=role_coding_artifact.validate_coding_artifact,
        stable_snapshot=True,
    ),
    "approved_memory_v1": ReadTool(
        tool_id="approved_memory_v1",
        version=1,
        description="Search approved memory visible to this profile's saved and current scope.",
        parameters=role_context_reads.MEMORY_PARAMETERS,
        parse_arguments=role_context_reads.parse_memory,
        execute=role_context_reads.approved_memory,
        validate_output=role_context_reads.validate_approved_memory,
    ),
    "approved_memory_v2": ReadTool(
        tool_id="approved_memory_v2",
        version=2,
        description="Search approved current facts with configured local semantic and exact-word retrieval.",
        parameters=role_context_reads.MEMORY_PARAMETERS,
        parse_arguments=role_context_reads.parse_memory,
        execute=_gateway_only,
        execute_with_gateway=_read_hybrid_memory,
        validate_output=role_context_reads.validate_approved_memory,
    ),
}

_CONTEXT_SQL = (
    "SELECT r.work_order_id,r.profile_id,r.status AS run_status,r.mode,"
    "w.status AS order_status,w.profile_id AS order_profile_id,"
    "p.status AS project_status,pr.allowed_tools AS current_tools,"
    "pr.name AS current_role_name,pr.kind AS current_role_kind,"
    "pr.memory_scope AS current_memory_scope,"
    "s.profile_id AS snapshot_profile_id,s.allowed_tools AS snapshot_tools,"
    "s.profile_name AS snapshot_role_name,"
    "s.memory_scope AS snapshot_memory_scope "
    "FROM runs r "
    "JOIN work_orders w ON w.project_id=r.project_id AND w.id=r.work_order_id "
    "JOIN projects p ON p.id=r.project_id "
    "JOIN profiles pr ON pr.project_id=r.project_id AND pr.id=r.profile_id "
    "JOIN LATERAL (SELECT profile_id,profile_name,allowed_tools,memory_scope FROM run_profile_snapshots "
    "WHERE project_id=r.project_id AND run_id=r.id "
    "ORDER BY transition_index DESC LIMIT 1) s ON true "
    "WHERE r.project_id=:project_id AND r.id=:run_id"
)
_LOCKED_CONTEXT_SQL = _CONTEXT_SQL + " FOR UPDATE OF r,w,p,pr"


def registered_tool_ids() -> frozenset[str]:
    """Canonical implemented role tools, independent of any profile grant."""
    from onefoundry.role_document_tools import DESCRIPTORS
    from onefoundry.role_zoho_mail import DESCRIPTORS as ZOHO_DESCRIPTORS

    return (
        frozenset(READ_TOOLS)
        | WRITE_TOOL_IDS
        | frozenset(role_web_tools.WEB_TOOLS)
        | MEDIA_TOOL_IDS
        | frozenset(DESCRIPTORS)
        | frozenset(ZOHO_DESCRIPTORS)
        | {"github_pr_read_v1", "github_pr_comment_v1", "browser_static_inspect_v1"}
    )


def _context(
    connection: Connection,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    *,
    lock: bool,
    max_items: int,
    allowed_modes: tuple[str, ...] = ("live_text", "synthetic_text"),
    allow_completed: bool = False,
) -> RunContext:
    row = (
        connection.execute(
            text(_LOCKED_CONTEXT_SQL if lock else _CONTEXT_SQL),
            {"project_id": project_id, "run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("run not found")
    active = row["run_status"] == "running" and row["order_status"] == "running"
    completed = allow_completed and row["run_status"] == "done" and row["order_status"] == "done"
    if (
        row["project_status"] != "active"
        or not (active or completed)
        or row["mode"] not in allowed_modes
        or row["profile_id"] != profile_id
        or row["order_profile_id"] != profile_id
        or row["snapshot_profile_id"] != profile_id
    ):
        raise ToolDenied("role tool run context is not active")
    return RunContext(
        project_id,
        run_id,
        row["work_order_id"],
        profile_id,
        frozenset(row["snapshot_tools"]),
        frozenset(row["current_tools"]),
        row["snapshot_memory_scope"],
        row["current_memory_scope"],
        max_items,
        row["current_role_name"],
        row["current_role_kind"],
        row["snapshot_role_name"],
    )


def _researcher(context: RunContext) -> bool:
    return (
        context.current_role_name == "Researcher"
        and context.current_role_kind == "builtin"
        and context.snapshot_role_name == "Researcher"
    )


def _db_bounds(connection: Connection, deadline: float) -> None:
    remaining_ms = int((deadline - monotonic()) * 1000)
    if remaining_ms <= 0:
        raise ToolTimedOut("role tool exceeded its configured timeout")
    duration = f"{remaining_ms}ms"
    connection.execute(text("SELECT set_config('lock_timeout', :duration, true)"), {"duration": duration})
    connection.execute(
        text("SELECT set_config('statement_timeout', :duration, true)"), {"duration": duration}
    )


def available_tools(
    gateway: ToolGateway, project_id: UUID, run_id: UUID, profile_id: UUID
) -> list[dict[str, Any]]:
    """Advertise only tools that would pass the same admission check now."""
    settings = gateway.live_text_settings
    if settings is None:
        return []
    deadline = monotonic() + settings.live_text_timeout_seconds
    try:
        with gateway.engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            _db_bounds(connection, deadline)
            context = _context(
                connection,
                project_id,
                run_id,
                profile_id,
                lock=False,
                max_items=settings.role_context_max_items,
            )
            allowed = context.snapshot_tools & context.current_tools
            reads = [
                {
                    "id": tool.tool_id,
                    "version": tool.version,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in READ_TOOLS.values()
                if tool.tool_id in allowed
            ]
            if settings.public_web_enabled and _researcher(context):
                reads.extend(
                    {
                        "id": tool.tool_id,
                        "version": tool.version,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                    for tool in role_web_tools.WEB_TOOLS.values()
                    if tool.tool_id in allowed
                    and (tool.tool_id != "public_web_search_v1" or settings.public_web_search_endpoint)
                )
            if "github_pr_read_v1" in allowed:
                from onefoundry.role_github_pr import PARAMETERS

                reads.append(
                    {
                        "id": "github_pr_read_v1",
                        "version": 1,
                        "description": "Read one approved GitHub PR against an exact saved source revision.",
                        "parameters": PARAMETERS,
                    }
                )
            if "github_pr_comment_v1" in allowed:
                from onefoundry.role_github_pr_comment import PARAMETERS

                reads.append(
                    {
                        "id": "github_pr_comment_v1",
                        "version": 1,
                        "description": "Propose one GitHub PR comment for operator approval; never post it.",
                        "parameters": PARAMETERS,
                    }
                )
            if "write_text_artifact_v1" in allowed:
                from onefoundry.role_artifact_write import PARAMETERS

                reads.append(
                    {
                        "id": "write_text_artifact_v1",
                        "version": 1,
                        "description": "Save a bounded private text, Markdown, or CSV artifact for this run.",
                        "parameters": PARAMETERS,
                    }
                )
            if settings.video_edit_enabled:
                from onefoundry.role_video_edit import descriptors

                reads.extend(item for item in descriptors() if item["id"] in allowed)
            if "request_video_capture_v1" in allowed:
                from onefoundry.role_video_capture import PARAMETERS as VIDEO_PARAMETERS

                reads.append(
                    {
                        "id": "request_video_capture_v1",
                        "version": 1,
                        "description": "Request local video recording, editing and playback review. "
                        "The operator selects the window; this tool does not launch recording.",
                        "parameters": VIDEO_PARAMETERS,
                    }
                )
            if "create_draft_work_order_v1" in allowed:
                from onefoundry.role_draft_work_order import PARAMETERS

                reads.append(
                    {
                        "id": "create_draft_work_order_v1",
                        "version": 1,
                        "description": "Create one draft work order for an existing profile in this project.",
                        "parameters": PARAMETERS,
                    }
                )
            if (
                "propose_profile_v1" in allowed
                and context.current_role_kind == "builtin"
                and context.current_role_name == "Chief of Staff"
            ):
                from onefoundry.coordination_profiles import PARAMETERS

                reads.append(
                    {
                        "id": "propose_profile_v1",
                        "version": 1,
                        "description": "Propose one new agent (and optionally its first draft work order) "
                        "for "
                        "operator approval. Tools must be within the operator's ceiling. Nothing is created "
                        "until the operator approves.",
                        "parameters": PARAMETERS,
                    }
                )
            if "propose_document_write_v1" in allowed:
                from onefoundry.document_writes import PARAMETERS

                reads.append(
                    {
                        "id": "propose_document_write_v1",
                        "version": 1,
                        "description": "Propose one new text, Markdown, or CSV file for a watched folder. "
                        "Nothing is written until the operator approves and sends it.",
                        "parameters": PARAMETERS,
                    }
                )
            if gateway.document_store is not None:
                from onefoundry.role_document_tools import DESCRIPTORS

                reads.extend(
                    {"id": tool_id, "version": 1, **descriptor}
                    for tool_id, descriptor in DESCRIPTORS.items()
                    if tool_id in allowed
                )
            if "propose_email_v1" in allowed:
                from onefoundry.smtp_outbound import PARAMETERS

                reads.append(
                    {
                        "id": "propose_email_v1",
                        "version": 1,
                        "description": "Propose one exact email for operator approval. "
                        "Approval triggers one SMTP submission attempt.",
                        "parameters": PARAMETERS,
                    }
                )
            from onefoundry.role_zoho_mail import DESCRIPTORS as ZOHO_DESCRIPTORS

            reads.extend(
                {"id": tool_id, "version": 1, **descriptor}
                for tool_id, descriptor in ZOHO_DESCRIPTORS.items()
                if tool_id in allowed
            )
            if "report_signal_v1" in allowed:
                from onefoundry.role_report_signal import PARAMETERS

                reads.append(
                    {
                        "id": "report_signal_v1",
                        "version": 1,
                        "description": "Report one finding (lead, risk, deadline, ...) for the operator's "
                        "automation rules or the Chief of Staff to handle. It does not start work.",
                        "parameters": PARAMETERS,
                    }
                )
            if "propose_extension_skill_v1" in allowed:
                from onefoundry.gateway_extension_skill_tool import PARAMETERS

                reads.append(
                    {
                        "id": "propose_extension_skill_v1",
                        "version": 1,
                        "description": "Propose a skill draft for operator review. Grants no permissions.",
                        "parameters": PARAMETERS,
                    }
                )
            if "request_qa_review_v1" in allowed:
                from onefoundry.role_qa_review_request import PARAMETERS

                reads.append(
                    {
                        "id": "request_qa_review_v1",
                        "version": 1,
                        "description": "Request QA review without changing the run owner or status.",
                        "parameters": PARAMETERS,
                    }
                )
            if settings.browser_static_enabled and getattr(gateway, "browser_static_jobs", None) is not None:
                from onefoundry.role_browser_static_inspect import TOOL_ID, descriptor, role_admitted

                if TOOL_ID in allowed and role_admitted(context):
                    reads.append(descriptor())
            from onefoundry.gateway_extension_registry import advertised
            from onefoundry.gateway_extension_worker_readiness import worker_status

            reads.extend(
                advertised(
                    connection,
                    project_id,
                    run_id,
                    profile_id,
                    allowed,
                    bool(worker_status(settings)["available"]),
                    settings.extension_worker_image,
                )
            )
            return reads
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in ("55P03", "57014"):
            raise ToolTimedOut("role tool discovery exceeded its configured timeout") from None
        raise ToolDependencyFailure("role tool discovery dependency failed") from None


def _argument_bytes(
    arguments: object, max_bytes: int, parse: Callable[[object], dict[str, Any]]
) -> tuple[dict[str, Any], bytes]:
    try:
        parsed = parse(arguments)
        encoded = json.dumps(parsed, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise ToolInputError("tool arguments do not match the registered contract") from None
    if len(encoded) > max_bytes:
        raise ToolInputError("tool arguments exceed the configured byte limit")
    return parsed, encoded


def _reject_sensitive_output(value: object, *, depth: int = 0) -> None:
    if depth > 10:
        raise ToolOutputRejected("tool result is too deeply nested")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ToolOutputRejected("tool result has an invalid field")
            normalized = "".join(char for char in key.casefold() if char.isalnum())
            if any(marker in normalized for marker in SENSITIVE_KEYS):
                raise ToolOutputRejected("tool result contains a sensitive field")
            _reject_sensitive_output(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_output(child, depth=depth + 1)
    elif isinstance(value, str) and contains_sensitive_text(value):
        raise ToolOutputRejected("tool result contains a secret pattern")
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ToolOutputRejected("tool result has a non-JSON value")


def _prior_attempt(connection: Connection, project_id: UUID, run_id: UUID, call_id: str) -> str | None:
    found = connection.scalar(
        text(
            "SELECT event_type FROM events WHERE project_id=:project_id AND run_id=:run_id "
            "AND event_type IN ('role_tool_started','role_tool_denied','role_tool_finished') "
            "AND payload->>'call_id'=:call_id ORDER BY created_at DESC,id DESC LIMIT 1"
        ),
        {"project_id": project_id, "run_id": run_id, "call_id": call_id},
    )
    return str(found) if found is not None else None


def _event(
    gateway: ToolGateway,
    connection: Connection,
    context: RunContext,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    gateway._event(
        connection,
        context.project_id,
        context.work_order_id,
        context.run_id,
        event_type,
        payload,
        actor_id=f"profile:{context.profile_id}",
    )


def invoke_tool(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    tool_id: str,
    arguments: object,
    *,
    tool_version: int,
) -> dict[str, Any]:
    """Record intent before a bounded read and record its result without raw data."""
    if not CALL_ID.fullmatch(call_id) or not TOOL_ID.fullmatch(tool_id):
        raise ToolInputError("tool call identifier is invalid")
    if type(tool_version) is not int or tool_version < 1:
        raise ToolInputError("tool version is invalid")
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    if tool_id.startswith("ext_"):
        from onefoundry.gateway_extension_runtime import invoke as invoke_extension

        return invoke_extension(
            gateway, project_id, run_id, profile_id, call_id, tool_id, arguments, tool_version
        )
    if tool_id == "propose_extension_skill_v1":
        from onefoundry.gateway_extension_skill_tool import invoke as propose_skill

        return propose_skill(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id in role_web_tools.WEB_TOOLS:
        from onefoundry.role_web_admission import invoke_web_tool

        return invoke_web_tool(
            gateway, project_id, run_id, profile_id, call_id, tool_id, arguments, tool_version
        )
    if tool_id == "github_pr_read_v1":
        from onefoundry.role_github_pr import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id == "github_pr_comment_v1":
        from onefoundry.role_github_pr_comment import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id == "write_text_artifact_v1":
        from onefoundry.role_artifact_write import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id in {"edit_video_v1", "video_edit_status_v1", "cancel_video_edit_v1"}:
        from onefoundry.role_video_edit import invoke as invoke_video_edit

        return invoke_video_edit(
            gateway, project_id, run_id, profile_id, call_id, tool_id, arguments, tool_version
        )
    if tool_id == "request_video_capture_v1":
        from onefoundry.role_video_capture import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id == "create_draft_work_order_v1":
        from onefoundry.role_draft_work_order import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id == "propose_profile_v1":
        from onefoundry.coordination_profiles import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id == "propose_document_write_v1":
        from onefoundry.document_writes import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id in {"document_search_v1", "document_range_v1", "propose_document_action_v1"}:
        from onefoundry.role_document_tools import invoke as invoke_document

        return invoke_document(
            gateway, project_id, run_id, profile_id, call_id, tool_id, arguments, tool_version
        )
    if tool_id == "propose_email_v1":
        from onefoundry.smtp_outbound import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id in {"zoho_mail_read_v1", "propose_zoho_mail_v1"}:
        from onefoundry.role_zoho_mail import invoke as invoke_zoho

        return invoke_zoho(gateway, project_id, run_id, profile_id, call_id, tool_id, arguments, tool_version)
    if tool_id == "report_signal_v1":
        from onefoundry.role_report_signal import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id == "request_qa_review_v1":
        from onefoundry.role_qa_review_request import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    if tool_id == "browser_static_inspect_v1":
        from onefoundry.role_browser_static_inspect import invoke

        return invoke(gateway, project_id, run_id, profile_id, call_id, arguments, tool_version)
    tool = READ_TOOLS.get(tool_id)
    deadline = monotonic() + settings.live_text_timeout_seconds
    parsed: dict[str, Any] = {}
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
                raise ToolOutcomeUnknown("tool call outcome is unknown; inspect the run timeline")
            if prior is not None:
                raise ToolDenied("tool call already attempted; inspect the run timeline")
            if (
                tool is None
                or tool.version != tool_version
                or tool_id not in context.snapshot_tools & context.current_tools
            ):
                _event(
                    gateway,
                    connection,
                    context,
                    "role_tool_denied",
                    {
                        "call_id": call_id,
                        "tool_id": tool_id,
                        "reason": "version_mismatch"
                        if tool is not None and tool.version != tool_version
                        else "not_admitted",
                    },
                )
                raise_after_commit = True
            else:
                raise_after_commit = False
                try:
                    parsed, encoded = _argument_bytes(
                        arguments, settings.live_text_max_prompt_bytes, tool.parse_arguments
                    )
                except ToolInputError:
                    _event(
                        gateway,
                        connection,
                        context,
                        "role_tool_denied",
                        {"call_id": call_id, "tool_id": tool_id, "reason": "invalid_arguments"},
                    )
                    raise_after_commit = True
                else:
                    _event(
                        gateway,
                        connection,
                        context,
                        "role_tool_started",
                        {
                            "call_id": call_id,
                            "tool_id": tool_id,
                            "version": tool.version,
                            "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
                            "argument_bytes": len(encoded),
                            "timeout_seconds": settings.live_text_timeout_seconds,
                        },
                    )
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in ("55P03", "57014"):
            raise ToolTimedOut("role tool exceeded its configured timeout") from None
        raise ToolDependencyFailure("role tool admission dependency failed") from None
    if raise_after_commit:
        raise ToolDenied("role tool call was denied")
    assert tool is not None
    result_ready = False
    try:
        with gateway.engine.begin() as lock_connection:
            _db_bounds(lock_connection, deadline)
            current = _context(
                lock_connection,
                project_id,
                run_id,
                profile_id,
                lock=True,
                max_items=settings.role_context_max_items,
            )
            if tool_id not in current.snapshot_tools & current.current_tools:
                raise ToolDenied("role tool permission changed before execution")
            with gateway.engine.begin() as read_connection:
                if tool.stable_snapshot:
                    read_connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                read_connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                _db_bounds(read_connection, deadline)
                if tool.execute_with_gateway is not None:
                    result = tool.execute_with_gateway(gateway, read_connection, current, parsed)
                else:
                    result = tool.execute(read_connection, current, parsed)
            if not isinstance(result, dict):
                raise ToolOutputRejected("tool result must be a JSON object")
            try:
                tool.validate_output(result)
            except ValueError:
                raise ToolOutputRejected("tool result failed its registered contract") from None
            encoded_result = json.dumps(result, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(encoded_result) > settings.live_text_max_output_bytes:
                raise ToolOutputRejected("tool result exceeds the configured byte limit")
            _reject_sensitive_output(result)
            if monotonic() > deadline:
                raise ToolTimedOut("role tool exceeded its configured timeout")
            digest = hashlib.sha256(encoded_result).hexdigest()
            result_ready = True
            _event(
                gateway,
                lock_connection,
                context,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": tool_id,
                    "status": "done",
                    "result_sha256": digest,
                    "result_bytes": len(encoded_result),
                },
            )
    except Exception as error:
        if result_ready:
            raise ToolOutcomeUnknown("role tool outcome could not be recorded") from None
        failure: ToolDenied | ToolOutputRejected | ToolTimedOut | ToolDependencyFailure
        if isinstance(error, (ToolDenied, ToolOutputRejected, ToolTimedOut)):
            failure = error
        elif isinstance(error, DBAPIError) and getattr(error.orig, "sqlstate", None) in ("55P03", "57014"):
            failure = ToolTimedOut("role tool exceeded its configured timeout")
        else:
            failure = ToolDependencyFailure("role tool dependency failed")
        reason = (
            "permission_changed"
            if isinstance(failure, ToolDenied)
            else "invalid_result"
            if isinstance(failure, ToolOutputRejected)
            else "timeout"
            if isinstance(failure, ToolTimedOut)
            else "dependency_failure"
        )
        try:
            with gateway.engine.begin() as connection:
                _db_bounds(connection, monotonic() + settings.live_text_timeout_seconds)
                _event(
                    gateway,
                    connection,
                    context,
                    "role_tool_finished",
                    {"call_id": call_id, "tool_id": tool_id, "status": "failed", "reason": reason},
                )
        except Exception:
            raise ToolOutcomeUnknown("role tool outcome could not be recorded") from None
        raise failure from None
    return {
        "call_id": call_id,
        "tool_id": tool_id,
        "version": tool.version,
        "status": "done",
        "result": result,
        "result_sha256": digest,
    }
