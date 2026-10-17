"""Claim an exact approved write before I/O; ambiguous results are never replayed."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.zoho_mail_actions import validate_binding
from onefoundry.zoho_mail_contracts import ZohoWrite
from onefoundry.zoho_mail_mcp import ZohoMcpError, provider_data
from onefoundry.zoho_mail_store import ZohoMailStore


def write_arguments(proposal: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    message = ZohoWrite.model_validate(proposal["message"])
    body: dict[str, Any] = {
        "fromAddress": proposal["from_address"],
        "toAddress": message.to_address,
        "subject": message.subject,
        "content": message.body,
        "mailFormat": "plaintext",
        "isSchedule": False,
    }
    paths = {"accountId": proposal["mail_account_id"]}
    if message.operation in {"draft", "reply_draft"}:
        body["mode"] = "draft"
    if message.operation.startswith("reply_"):
        paths["messageId"] = message.message_id
        body["action"] = "reply"
        tool = "sendReplyEmail"
    else:
        tool = "sendEmail"
    return tool, {"path_variables": paths, "body": body}


def _finish(
    store: ZohoMailStore,
    project_id: UUID,
    action: dict[str, Any],
    state: str,
    error: str | None,
    message_id: str | None = None,
) -> None:
    with store.gateway.engine.begin() as connection:
        updated = connection.execute(
            text(
                "UPDATE zoho_mail_actions SET state=:state,error_code=:error,message_id=:message,"
                "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id AND state='dispatching'"
            ),
            {"state": state, "error": error, "message": message_id, "p": project_id, "id": action["id"]},
        )
        if updated.rowcount == 1:
            store.gateway._event(
                connection,
                project_id,
                action["work_order_id"],
                action["run_id"],
                f"zoho_mail_{state}",
                {"action_id": str(action["id"]), "error_code": error, "message_id": message_id},
            )


def dispatch_once(store: ZohoMailStore, project_id: UUID, action_id: UUID) -> None:
    if store.gateway.actor_id != store.settings.operator_actor_id:
        raise Conflict("only the operator may dispatch approved Zoho mail")
    with store.gateway.engine.begin() as connection:
        row = (
            connection.execute(
                text("SELECT * FROM zoho_mail_actions WHERE project_id=:p AND id=:id FOR UPDATE"),
                {"p": project_id, "id": action_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("Zoho mail action not found")
        action = dict(row)
        if action["state"] != "approved":
            raise Conflict("Zoho mail is not approved or was already attempted")
        approval = (
            connection.execute(
                text("SELECT * FROM approval_requests WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": action["approval_id"]},
            )
            .mappings()
            .one()
        )
        if approval["status"] != "approved":
            raise Conflict("Zoho mail needs exact operator approval")
        validate_binding(action, dict(approval))
        current = store.ready(connection, project_id, action["connection_id"])
        if (
            current["version"] != action["connection_version"]
            or connection.scalar(text("SELECT status FROM projects WHERE id=:p"), {"p": project_id})
            != "active"
        ):
            raise Conflict("Zoho connection or project changed after approval")
        tool, arguments = write_arguments(action["proposal"])
        client = store.client(current)
        connection.execute(
            text("UPDATE zoho_mail_actions SET state='dispatching' WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": action_id},
        )
        store.gateway._event(
            connection,
            project_id,
            action["work_order_id"],
            action["run_id"],
            "zoho_mail_dispatching",
            {"action_id": str(action_id)},
        )
    try:
        receipt = provider_data(client.call(tool, arguments))
        if not isinstance(receipt, dict):
            raise ZohoMcpError("unconfirmed_receipt")
        message_id = str(receipt.get("messageId", ""))
        if not message_id.isdigit() or len(message_id) > 30:
            raise ZohoMcpError("unconfirmed_receipt")
    except (ZohoMcpError, Unavailable) as error:
        if isinstance(error, ZohoMcpError) and error.code == "authentication_failed":
            store.authentication_failed(project_id, current)
        _finish(
            store,
            project_id,
            action,
            "unknown",
            error.code if isinstance(error, ZohoMcpError) else "credential_unavailable",
        )
    else:
        # A provider receipt establishes acceptance, not recipient delivery.
        _finish(store, project_id, action, "accepted", None, message_id)


def list_actions(store: ZohoMailStore, project_id: UUID) -> list[dict[str, Any]]:
    with store.gateway.engine.connect() as connection:
        store.gateway._require_project(connection, project_id)
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT id,connection_id,approval_id,state,error_code,message_id,created_at,"
                    "proposal->'message'->>'operation' AS operation "
                    "FROM zoho_mail_actions WHERE project_id=:p ORDER BY created_at DESC,id"
                ),
                {"p": project_id},
            ).mappings()
        ]


def reconcile_interrupted(store: ZohoMailStore) -> None:
    """Single app startup: a previously claimed write is unknown, never queued again."""
    with store.gateway.engine.begin() as connection:
        rows = (
            connection.execute(
                text(
                    "UPDATE zoho_mail_actions SET state='unknown',error_code='dispatch_interrupted',"
                    "finished_at=clock_timestamp() WHERE state='dispatching' "
                    "RETURNING id,project_id,work_order_id,run_id"
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            store.gateway._event(
                connection,
                row["project_id"],
                row["work_order_id"],
                row["run_id"],
                "zoho_mail_unknown",
                {"action_id": str(row["id"]), "error_code": "dispatch_interrupted"},
            )
