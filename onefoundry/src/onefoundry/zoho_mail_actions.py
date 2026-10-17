"""Exact Zoho mail proposals; approval records bind the connection and complete message."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.zoho_mail_contracts import ZohoWrite
from onefoundry.zoho_mail_store import ZohoMailStore


def digest(proposal: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            proposal, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def display(proposal: dict[str, Any]) -> str:
    data = ZohoWrite.model_validate(proposal["message"])
    return (
        f"Zoho Mail: {data.operation}\nFrom: {proposal['from_address']}\nTo: {data.to_address}\n"
        f"Subject: {data.subject}\nReply to message: {data.message_id or '(new message)'}\n\n{data.body}"
    )


def save(
    store: ZohoMailStore,
    connection: Any,
    project_id: UUID,
    run_id: UUID,
    work_order_id: UUID,
    actor_id: str,
    data: ZohoWrite,
) -> dict[str, Any]:
    row = store.ready(connection, project_id, data.connection_id)
    proposal = {
        "message": data.model_dump(mode="json"),
        "connection_version": row["version"],
        "from_address": row["from_address"],
        "mail_account_id": row["mail_account_id"],
    }
    checksum, approval_id, action_id = digest(proposal), uuid4(), uuid4()
    connection.execute(
        text(
            "INSERT INTO approval_requests (id,project_id,actor_id,work_order_id,run_id,action_type,"
            "target,content,proposal_digest,status) VALUES "
            "(:id,:p,:actor,:o,:r,'email',:target,:content,:digest,'pending')"
        ),
        {
            "id": approval_id,
            "p": project_id,
            "actor": actor_id,
            "o": work_order_id,
            "r": run_id,
            "target": data.to_address,
            "content": display(proposal),
            "digest": checksum,
        },
    )
    connection.execute(
        text(
            "INSERT INTO zoho_mail_actions (id,project_id,actor_id,connection_id,connection_version,"
            "work_order_id,run_id,approval_id,proposal,proposal_digest) VALUES "
            "(:id,:p,:actor,:c,:v,:o,:r,:a,:proposal,:digest)"
        ).bindparams(bindparam("proposal", type_=JSONB)),
        {
            "id": action_id,
            "p": project_id,
            "actor": actor_id,
            "c": row["id"],
            "v": row["version"],
            "o": work_order_id,
            "r": run_id,
            "a": approval_id,
            "proposal": proposal,
            "digest": checksum,
        },
    )
    store.gateway._event(
        connection,
        project_id,
        work_order_id,
        run_id,
        "zoho_mail_proposed",
        {
            "action_id": str(action_id),
            "approval_id": str(approval_id),
            "operation": data.operation,
            "proposal_digest": checksum,
        },
        actor_id=actor_id,
    )
    return {"action_id": str(action_id), "approval_id": str(approval_id), "state": "pending"}


def validate_binding(action: dict[str, Any], approval: dict[str, Any]) -> None:
    proposal = action["proposal"]
    try:
        checksum, displayed = digest(proposal), display(proposal)
    except (ValueError, KeyError, TypeError):
        raise Conflict("Zoho mail proposal is malformed; reject and prepare it again") from None
    if (
        approval["action_type"] != "email"
        or action["proposal_digest"] != checksum
        or approval["proposal_digest"] != checksum
        or approval["content"] != displayed
        or approval["target"] != proposal["message"]["to_address"]
        or action["connection_version"] != proposal["connection_version"]
        or str(action["connection_id"]) != proposal["message"]["connection_id"]
    ):
        raise Conflict("Zoho mail proposal no longer matches the approved message")


def apply_decision(
    store: ZohoMailStore, connection: Any, project_id: UUID, approval: dict[str, Any], decision: str
) -> UUID | None:
    row = (
        connection.execute(
            text(
                "SELECT * FROM zoho_mail_actions WHERE project_id=:p "
                "AND approval_id=:a AND state='pending' FOR UPDATE"
            ),
            {"p": project_id, "a": approval["id"]},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    action = dict(row)
    validate_binding(action, approval)
    if decision == "approved":
        current = store.ready(connection, project_id, action["connection_id"])
        if current["version"] != action["connection_version"]:
            raise Conflict("Zoho connection changed; reject and prepare a new mail proposal")
    state = "approved" if decision == "approved" else "rejected"
    connection.execute(
        text("UPDATE zoho_mail_actions SET state=:s WHERE project_id=:p AND id=:id"),
        {"s": state, "p": project_id, "id": action["id"]},
    )
    store.gateway._event(
        connection,
        project_id,
        action["work_order_id"],
        action["run_id"],
        f"zoho_mail_{state}",
        {"action_id": str(action["id"])},
    )
    return action["id"] if decision == "approved" else None


def propose_manual(store: ZohoMailStore, project_id: UUID, run_id: UUID, data: ZohoWrite) -> dict[str, Any]:
    with store.gateway.engine.begin() as connection:
        run = (
            connection.execute(
                text("SELECT work_order_id,status,mode FROM runs WHERE project_id=:p AND id=:r FOR UPDATE"),
                {"p": project_id, "r": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise NotFound("run not found")
        if run["status"] != "running" or run["mode"] != "manual_planning":
            raise Conflict("mail proposal requires a running manual work order")
        store.gateway._require_unblocked_order(connection, project_id, run["work_order_id"])
        return save(store, connection, project_id, run_id, run["work_order_id"], store.gateway.actor_id, data)
