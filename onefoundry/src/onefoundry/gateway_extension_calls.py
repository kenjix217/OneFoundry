"""Durable exact-call claims and approval bindings owned by Tool Gateway."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.gateway_extension_registry import ExtensionTool, digest
from onefoundry.role_tool_admission import RunContext, ToolDenied, ToolOutcomeUnknown

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _approval(
    gateway: ToolGateway,
    connection: Connection,
    context: RunContext,
    invocation_id: UUID,
    tool: ExtensionTool,
    arguments: dict[str, Any],
) -> UUID:
    approval_id = uuid4()
    title = f"Allow {tool.manifest.name}: {tool.spec.id} ({tool.spec.effect})"
    config = tool.connection["config"] if tool.connection else {}
    content = json.dumps(
        {
            "kind": "extension_tool_invocation_v1",
            "invocation_id": str(invocation_id),
            "package_sha256": tool.package_sha256,
            "tool": tool.id,
            "destination": config.get("endpoint"),
            "operation": tool.spec.operation,
            "connection_version": tool.connection_version,
            "binding_version": tool.binding_version,
            "arguments": arguments,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    proposal = {"action_type": "other", "target": title, "content": content}
    connection.execute(
        text(
            "INSERT INTO approval_requests "
            "(id,project_id,actor_id,work_order_id,run_id,action_type,target,content,proposal_digest,status) "
            "VALUES (:id,:p,:actor,:order,:run,'other',:target,:content,:digest,'pending')"
        ),
        {
            "id": approval_id,
            "p": context.project_id,
            "actor": f"profile:{context.profile_id}",
            "order": context.work_order_id,
            "run": context.run_id,
            "target": title,
            "content": content,
            "digest": digest(proposal),
        },
    )
    gateway._event(
        connection,
        context.project_id,
        context.work_order_id,
        context.run_id,
        "approval_requested",
        {"approval_id": str(approval_id), "action_type": "other", "proposal_digest": digest(proposal)},
        actor_id=f"profile:{context.profile_id}",
    )
    return approval_id


def claim(
    gateway: ToolGateway,
    connection: Connection,
    context: RunContext,
    tool: ExtensionTool,
    call_id: str,
    arguments: dict[str, Any],
    *,
    execute_approved: bool,
) -> tuple[dict[str, Any], bool]:
    request_hash = digest(
        {
            "tool": tool.id,
            "package": tool.package_sha256,
            "descriptor": tool.descriptor_sha256,
            "connection": str(tool.connection_id),
            "version": tool.connection_version,
            "binding_version": tool.binding_version,
            "arguments": arguments,
        }
    )
    prior = (
        connection.execute(
            text(
                "SELECT * FROM extension_invocations WHERE project_id=:p AND run_id=:run "
                "AND call_id=:call FOR UPDATE"
            ),
            {"p": context.project_id, "run": context.run_id, "call": call_id, "hash": request_hash},
        )
        .mappings()
        .all()
    )
    if len(prior) > 1:
        raise ToolDenied("tool call identity conflicts with an earlier invocation")
    if prior:
        saved = dict(prior[0])
        if saved["request_sha256"] != request_hash:
            raise ToolDenied("call ID was already bound to different arguments")
        if saved["status"] == "unknown":
            raise ToolOutcomeUnknown("extension outcome is unknown; reconcile the saved invocation")
        if saved["status"] in {"failed", "cancelled"}:
            raise ToolDenied("extension invocation is terminal; inspect its receipt")
        if saved["status"] in {"running", "ready", "waiting", "done"}:
            return saved, False
        if saved["approval_id"]:
            approval = (
                connection.execute(
                    text(
                        "SELECT status,content FROM approval_requests "
                        "WHERE project_id=:p AND id=:id FOR SHARE"
                    ),
                    {"p": context.project_id, "id": saved["approval_id"]},
                )
                .mappings()
                .one()
            )
            if approval["status"] == "rejected":
                raise ToolDenied("operator rejected this exact invocation")
            expected = {
                "kind": "extension_tool_invocation_v1",
                "invocation_id": str(saved["id"]),
                "package_sha256": tool.package_sha256,
                "tool": tool.id,
                "destination": (tool.connection["config"] if tool.connection else {}).get("endpoint"),
                "operation": tool.spec.operation,
                "connection_version": tool.connection_version,
                "binding_version": tool.binding_version,
                "arguments": arguments,
            }
            approved = approval["status"] == "approved" and digest(json.loads(approval["content"])) == digest(
                expected
            )
            if not approved or not execute_approved:
                return saved, False
        saved["status"] = "running"
        connection.execute(
            text(
                "UPDATE extension_invocations SET status='running',started_at=clock_timestamp() WHERE id=:id"
            ),
            {"id": saved["id"]},
        )
        return saved, True
    invocation_id = uuid4()
    needs_review = tool.spec.runtime != "python" or tool.spec.effect != "pure"
    approval_id = (
        _approval(gateway, connection, context, invocation_id, tool, arguments) if needs_review else None
    )
    status = "approval_required" if needs_review else "running"
    connection.execute(
        text(
            "INSERT INTO extension_invocations "
            "(id,project_id,actor_id,run_id,profile_id,tool_id,call_id,request_sha256,package_id,"
            "package_sha256,descriptor_sha256,connection_id,connection_version,arguments,"
            "approval_id,status,started_at) "
            "VALUES (:id,:p,:actor,:run,:profile,:tool,:call,:request,:package,:package_hash,:descriptor,"
            ":connection,:version,:arguments,:approval,:status,"
            "CASE WHEN :status='running' THEN clock_timestamp() ELSE NULL END)"
        ).bindparams(bindparam("arguments", type_=JSONB)),
        {
            "id": invocation_id,
            "p": context.project_id,
            "actor": f"profile:{context.profile_id}",
            "run": context.run_id,
            "profile": context.profile_id,
            "tool": tool.id,
            "call": call_id,
            "request": request_hash,
            "package": tool.package_id,
            "package_hash": tool.package_sha256,
            "descriptor": tool.descriptor_sha256,
            "connection": tool.connection_id,
            "version": tool.connection_version,
            "arguments": arguments,
            "approval": approval_id,
            "status": status,
        },
    )
    saved = {
        "id": invocation_id,
        "project_id": context.project_id,
        "run_id": context.run_id,
        "profile_id": context.profile_id,
        "tool_id": tool.id,
        "call_id": call_id,
        "package_sha256": tool.package_sha256,
        "arguments": arguments,
        "approval_id": approval_id,
        "status": status,
        "result": None,
    }
    gateway._event(
        connection,
        context.project_id,
        context.work_order_id,
        context.run_id,
        "extension_invocation_created",
        {
            "invocation_id": str(invocation_id),
            "tool_id": tool.id,
            "status": status,
            "arguments_sha256": digest(arguments),
        },
        actor_id=f"profile:{context.profile_id}",
    )
    return saved, not needs_review


def lock_invocation_run(connection: Connection, project_id: UUID, invocation_id: UUID) -> None:
    connection.execute(
        text(
            "SELECT id FROM runs WHERE project_id=:p AND id=(SELECT run_id FROM extension_invocations "
            "WHERE project_id=:p AND id=:id) FOR UPDATE"
        ),
        {"p": project_id, "id": invocation_id},
    )


def settle(
    gateway: ToolGateway,
    project_id: UUID,
    invocation_id: UUID,
    status: str,
    result: dict[str, Any] | None = None,
    reason: str | None = None,
    waiting_invocation_id: UUID | None = None,
    expected_status: str = "running",
) -> None:
    with gateway.engine.begin() as connection:
        lock_invocation_run(connection, project_id, invocation_id)
        row = (
            connection.execute(
                text(
                    "UPDATE extension_invocations SET status=:status,result=:result,reason=:reason,"
                    "waiting_invocation_id=coalesce(:waiting_id,waiting_invocation_id),"
                    "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id AND status=:expected "
                    "RETURNING run_id,profile_id,tool_id,"
                    "(SELECT work_order_id FROM runs WHERE runs.id=extension_invocations.run_id "
                    "AND runs.project_id=extension_invocations.project_id) AS work_order_id"
                ).bindparams(bindparam("result", type_=JSONB)),
                {
                    "p": project_id,
                    "id": invocation_id,
                    "status": status,
                    "result": result,
                    "reason": reason,
                    "waiting_id": waiting_invocation_id,
                    "expected": expected_status,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ToolOutcomeUnknown("extension invocation could not be settled")
        gateway._event(
            connection,
            project_id,
            row["work_order_id"],
            row["run_id"],
            "extension_invocation_finished",
            {
                "invocation_id": str(invocation_id),
                "tool_id": row["tool_id"],
                "status": status,
                "result_sha256": digest(result) if result is not None else None,
                "reason": reason,
                "waiting_invocation_id": str(waiting_invocation_id) if waiting_invocation_id else None,
            },
            actor_id=f"profile:{row['profile_id']}",
        )


def public_receipt(saved: dict[str, Any]) -> dict[str, Any]:
    return {
        "invocation_id": str(saved["id"]),
        "tool_id": saved["tool_id"],
        "version": 1,
        "status": saved["status"],
        "approval_id": str(saved["approval_id"]) if saved.get("approval_id") else None,
        "result": saved.get("result"),
        "reason": saved.get("reason"),
        "waiting_invocation_id": str(saved["waiting_invocation_id"])
        if saved.get("waiting_invocation_id")
        else None,
    }
