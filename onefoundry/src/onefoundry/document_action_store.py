"""Immutable proposals, approval transitions, and single dispatch claims."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.document_access import digest
from onefoundry.gateway_errors import Conflict, NotFound

if TYPE_CHECKING:
    from onefoundry.role_tool_admission import RunContext
    from onefoundry.tool_gateway import ToolGateway

PUBLIC = (
    "id,source_id,work_order_id,run_id,approval_id,file_id,file_name,operation,state,error_code,"
    "readback,created_at,finished_at"
)


def save(
    gateway: ToolGateway, connection: Any, context: RunContext, proposal: dict[str, Any]
) -> dict[str, str]:
    data, snapshot = proposal["action"], proposal["snapshot"]
    action_id, approval_id = uuid4(), uuid4()
    checksum = digest(proposal)
    preview = f"File: {snapshot['name']}\nSelected folder: {proposal['folder_label']}\n"
    if data["operation"] == "replace_file":
        preview += (
            f"Replacement: {proposal['input']['name']} ({proposal['input']['byte_size']} bytes)\n"
            f"Exact replacement SHA-256: {proposal['input']['sha256']}"
        )
    elif data["operation"] == "edit_cells":
        preview += (
            f"Cells: {data['sheet']}!{data['address']}\n"
            f"Before:\n{json.dumps(proposal['before'], ensure_ascii=False)}\n"
            f"After:\n{json.dumps(data['values'], ensure_ascii=False)}"
        )
    else:
        preview += (
            f"Recipient: {data['email']}\nAccess: {'Can edit' if data['access'] == 'write' else 'Can read'}\n"
            "Existing and inherited permissions are retained. No notification email will be sent."
        )
    preview += (
        "\n\nThe file revision is checked again before dispatch. External editors can still change "
        "a file during dispatch; readback verifies the requested result, not an atomic lock on "
        "external editors."
    )
    if len(preview) > 15500:
        raise Conflict("proposal is too large to show completely in the approval inbox")
    description = {
        "replace_file": "Upload a new version",
        "edit_cells": "Edit exact cells",
        "share_file": "Grant access without an email notification",
    }[data["operation"]]
    connection.execute(
        text(
            "INSERT INTO approval_requests (id,project_id,actor_id,work_order_id,run_id,action_type,target,"
            "content,proposal_digest,status) "
            "VALUES (:id,:p,:actor,:o,:r,'publish',:target,:content,:digest,'pending')"
        ),
        {
            "id": approval_id,
            "p": context.project_id,
            "actor": f"profile:{context.profile_id}",
            "o": context.work_order_id,
            "r": context.run_id,
            "target": snapshot["name"][:500],
            "content": description + "\n\n" + preview,
            "digest": checksum,
        },
    )
    connection.execute(
        text(
            "INSERT INTO document_actions (id,project_id,actor_id,source_id,work_order_id,run_id,approval_id,"
            "file_id,file_name,operation,proposal,proposal_digest) "
            "VALUES (:id,:p,:actor,:s,:o,:r,:a,:f,:name,:op,:proposal,:digest)"
        ).bindparams(bindparam("proposal", type_=JSONB)),
        {
            "id": action_id,
            "p": context.project_id,
            "actor": f"profile:{context.profile_id}",
            "s": UUID(data["source_id"]),
            "o": context.work_order_id,
            "r": context.run_id,
            "a": approval_id,
            "f": data["file_id"],
            "name": snapshot["name"],
            "op": data["operation"],
            "proposal": proposal,
            "digest": checksum,
        },
    )
    gateway._event(
        connection,
        context.project_id,
        context.work_order_id,
        context.run_id,
        "document_action_proposed",
        {
            "action_id": str(action_id),
            "approval_id": str(approval_id),
            "operation": data["operation"],
            "proposal_digest": checksum,
        },
        actor_id=f"profile:{context.profile_id}",
    )
    return {"action_id": str(action_id), "approval_id": str(approval_id), "status": "pending"}


def apply_decision(
    gateway: ToolGateway, connection: Any, project_id: UUID, approval: dict[str, Any], decision: str
) -> None:
    row = (
        connection.execute(
            text(
                "SELECT id,proposal,proposal_digest FROM document_actions "
                "WHERE project_id=:p AND approval_id=:a AND state='pending' FOR UPDATE"
            ),
            {"p": project_id, "a": approval["id"]},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return
    if (
        digest(row["proposal"]) != row["proposal_digest"]
        or approval["proposal_digest"] != row["proposal_digest"]
    ):
        raise Conflict("document action no longer matches the exact approval")
    state = "approved" if decision == "approved" else "rejected"
    connection.execute(
        text("UPDATE document_actions SET state=:state WHERE project_id=:p AND id=:id"),
        {"state": state, "p": project_id, "id": row["id"]},
    )
    gateway._event(
        connection, project_id, None, None, f"document_action_{state}", {"action_id": str(row["id"])}
    )


def list_actions(gateway: ToolGateway, project_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    f"SELECT {PUBLIC} FROM document_actions WHERE project_id=:p ORDER BY created_at DESC,id"  # noqa: S608
                ),
                {"p": project_id},
            ).mappings()
        ]  # noqa: S608


def claim(gateway: ToolGateway, project_id: UUID, action_id: UUID) -> dict[str, Any]:
    with gateway.engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT d.*,a.status AS approval_status,a.proposal_digest AS approved_digest,"
                    "s.connector_account_id FROM document_actions d JOIN approval_requests a "
                    "ON a.project_id=d.project_id AND a.id=d.approval_id JOIN document_sources s "
                    "ON s.project_id=d.project_id AND s.id=d.source_id "
                    "WHERE d.project_id=:p AND d.id=:id FOR UPDATE OF d"
                ),
                {"p": project_id, "id": action_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("document action not found")
        if row["state"] != "approved" or row["approval_status"] != "approved":
            raise Conflict("only an approved, unattempted action can be sent")
        if (
            digest(row["proposal"]) != row["proposal_digest"]
            or row["approved_digest"] != row["proposal_digest"]
        ):
            raise Conflict("document action no longer matches its approval")
        key = f"{project_id}:{row['connector_account_id']}:{row['file_id']}"
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key})
        busy = connection.scalar(
            text(
                "SELECT 1 FROM document_actions d JOIN document_sources s "
                "ON s.project_id=d.project_id AND s.id=d.source_id WHERE d.project_id=:p AND d.file_id=:f "
                "AND s.connector_account_id=:account AND d.state IN ('dispatching','unknown') LIMIT 1"
            ),
            {"p": project_id, "f": row["file_id"], "account": row["connector_account_id"]},
        )
        if busy:
            raise Conflict(
                "another action on this file is in progress or has an unknown outcome; inspect it first"
            )
        connection.execute(
            text("UPDATE document_actions SET state='dispatching' WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": action_id},
        )
        gateway._event(
            connection,
            project_id,
            row["work_order_id"],
            row["run_id"],
            "document_action_dispatching",
            {"action_id": str(action_id), "operation": row["operation"]},
        )
        return dict(row)


def finish(
    gateway: ToolGateway,
    project_id: UUID,
    action_id: UUID,
    state: str,
    error: str | None,
    readback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with gateway.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE document_actions SET state=:state,error_code=:error,readback=:readback,"
                "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id AND state='dispatching'"
            ).bindparams(bindparam("readback", type_=JSONB(none_as_null=True))),
            {"state": state, "error": error, "readback": readback, "p": project_id, "id": action_id},
        )
        gateway._event(
            connection,
            project_id,
            None,
            None,
            f"document_action_{state}",
            {"action_id": str(action_id), "error_code": error},
        )
    return next(row for row in list_actions(gateway, project_id) if row["id"] == action_id)
