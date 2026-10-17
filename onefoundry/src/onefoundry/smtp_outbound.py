"""Exact outbound email proposals and one-shot, approval-triggered SMTP submission."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.smtp_accounts import SmtpAccounts
from onefoundry.smtp_schemas import OutboundProposal, address
from onefoundry.workflow_schemas import StrictModel, local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

TOOL_ID = "propose_email_v1"
COLUMNS = (
    "id,account_id,work_order_id,run_id,approval_id,to_address,subject,body,state,"
    "reason_code,attempted_at,finished_at,created_at"
)
PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "account_id": {"type": "string", "format": "uuid"},
        "to_address": {"type": "string", "maxLength": 320},
        "subject": {"type": "string", "maxLength": 200},
        "body": {"type": "string", "maxLength": 12000},
    },
    "required": ["account_id", "to_address", "subject", "body"],
    "additionalProperties": False,
}


class AgentEmail(StrictModel):
    account_id: UUID
    to_address: str
    subject: str
    body: str


def parse_arguments(value: object) -> dict[str, Any]:
    try:
        data = AgentEmail.model_validate(value)
        recipient = address(data.to_address)
        subject = local_text(data.subject)
        body = local_text(data.body)
        if not subject or len(subject) > 200 or "\r" in subject or "\n" in subject:
            raise ValueError("subject must be one line and at most 200 characters")
        if not body or len(body.encode()) > 12000 or contains_sensitive_text(body):
            raise ValueError("email body is empty, too long, or appears to contain a secret")
    except (ValidationError, ValueError, TypeError):
        from onefoundry.role_tool_admission import ToolInputError

        raise ToolInputError("email proposal arguments are invalid") from None
    return {"account_id": str(data.account_id), "to_address": recipient, "subject": subject, "body": body}


def _digest(
    account_id: UUID, account_version: int, sender: str, recipient: str, subject: str, body: str
) -> str:
    value = {
        "account_id": str(account_id),
        "account_version": account_version,
        "from_address": sender,
        "to_address": recipient,
        "subject": subject,
        "body": body,
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _save(
    gateway: ToolGateway,
    connection: Any,
    accounts: SmtpAccounts,
    project_id: UUID,
    run_id: UUID,
    work_order_id: UUID,
    actor_id: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    account = accounts._row(connection, project_id, UUID(fields["account_id"]), lock=True)
    if account["status"] != "ready" or not account["enabled"] or not accounts.secrets.present(account["id"]):
        raise Conflict("SMTP account must be checked and enabled before proposing email")
    digest = _digest(
        account["id"],
        account["version"],
        account["from_address"],
        fields["to_address"],
        fields["subject"],
        fields["body"],
    )
    approval_id, email_id = uuid4(), uuid4()
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
            "target": fields["to_address"],
            "content": (
                f"From: {account['from_address']}\nTo: {fields['to_address']}\n"
                f"Subject: {fields['subject']}\n\n{fields['body']}"
            ),
            "digest": digest,
        },
    )
    connection.execute(
        text(
            "INSERT INTO outbound_emails (id,project_id,actor_id,account_id,account_version,work_order_id,"
            "run_id,approval_id,to_address,subject,body,content_sha256) VALUES "
            "(:id,:p,:actor,:account,:version,:o,:r,:approval,:recipient,:subject,:body,:sha)"
        ),
        {
            "id": email_id,
            "p": project_id,
            "actor": actor_id,
            "account": account["id"],
            "version": account["version"],
            "o": work_order_id,
            "r": run_id,
            "approval": approval_id,
            "recipient": fields["to_address"],
            "subject": fields["subject"],
            "body": fields["body"],
            "sha": hashlib.sha256(fields["body"].encode()).hexdigest(),
        },
    )
    gateway._event(
        connection,
        project_id,
        work_order_id,
        run_id,
        "outbound_email_proposed",
        {"email_id": str(email_id), "approval_id": str(approval_id), "proposal_digest": digest},
        actor_id=actor_id,
    )
    return {"email_id": str(email_id), "approval_id": str(approval_id), "state": "pending"}


def propose_manual(
    gateway: ToolGateway, accounts: SmtpAccounts, project_id: UUID, data: OutboundProposal
) -> dict[str, Any]:
    fields = {
        "account_id": str(data.account_id),
        "to_address": data.to_address,
        "subject": data.subject,
        "body": data.body,
    }
    with gateway.engine.begin() as connection:
        run = (
            connection.execute(
                text("SELECT work_order_id,status,mode FROM runs WHERE project_id=:p AND id=:r FOR UPDATE"),
                {"p": project_id, "r": data.run_id},
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise NotFound("run not found")
        if run["status"] != "running" or run["mode"] != "manual_planning":
            raise Conflict("manual email proposal requires a running manual work order")
        gateway._require_unblocked_order(connection, project_id, run["work_order_id"])
        return _save(
            gateway,
            connection,
            accounts,
            project_id,
            data.run_id,
            run["work_order_id"],
            gateway.actor_id,
            fields,
        )


def invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
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

    if not CALL_ID.fullmatch(call_id) or contains_sensitive_text(call_id):
        raise ToolInputError("tool call identifier is invalid")
    if type(tool_version) is not int or tool_version != 1:
        raise ToolDenied("email tool version is not admitted")
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("role tool settings are unavailable")
    from time import monotonic

    try:
        with gateway.engine.begin() as connection:
            _db_bounds(connection, monotonic() + settings.live_text_timeout_seconds)
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
                raise ToolOutcomeUnknown("email proposal outcome is unknown; inspect the run timeline")
            if prior is not None:
                raise ToolDenied("email proposal call already attempted")
            if TOOL_ID not in context.snapshot_tools & context.current_tools:
                raise ToolDenied("email proposal is not admitted for this run")
            fields, encoded = _argument_bytes(arguments, settings.live_text_max_prompt_bytes, parse_arguments)
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
            result = _save(
                gateway,
                connection,
                SmtpAccounts(gateway, settings),
                project_id,
                run_id,
                context.work_order_id,
                f"profile:{profile_id}",
                fields,
            )
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
            "email proposal transaction outcome is unknown; inspect the run timeline"
        ) from None


def apply_decision(
    gateway: ToolGateway, connection: Any, project_id: UUID, approval: dict[str, Any], decision: str
) -> tuple[bool, UUID | None]:
    row = (
        connection.execute(
            text(
                "SELECT e.*,a.from_address,a.version AS current_version,a.enabled FROM outbound_emails e "
                "JOIN smtp_accounts a ON a.project_id=e.project_id AND a.id=e.account_id "
                "WHERE e.project_id=:p AND e.approval_id=:a AND e.state='pending' FOR UPDATE OF e,a"
            ),
            {"p": project_id, "a": approval["id"]},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return False, None
    expected = _digest(
        row["account_id"],
        row["account_version"],
        row["from_address"],
        row["to_address"],
        row["subject"],
        row["body"],
    )
    if (
        approval["action_type"] != "email"
        or approval["proposal_digest"] != expected
        or hashlib.sha256(row["body"].encode()).hexdigest() != row["content_sha256"]
    ):
        raise Conflict("email no longer matches its approval request")
    if decision == "approved" and (not row["enabled"] or row["current_version"] != row["account_version"]):
        raise Conflict("SMTP account changed or was disabled; reject and propose a new email")
    state = "approved" if decision == "approved" else "rejected"
    connection.execute(
        text("UPDATE outbound_emails SET state=:s WHERE project_id=:p AND id=:id"),
        {"s": state, "p": project_id, "id": row["id"]},
    )
    gateway._event(
        connection,
        project_id,
        row["work_order_id"],
        row["run_id"],
        f"outbound_email_{state}",
        {"email_id": str(row["id"]), "approval_id": str(approval["id"])},
    )
    return True, row["id"] if decision == "approved" else None


def list_emails(gateway: ToolGateway, project_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        gateway._require_project(connection, project_id)
        return [
            dict(row)
            for row in connection.execute(
                text(
                    f"SELECT {COLUMNS} FROM outbound_emails "  # noqa: S608
                    "WHERE project_id=:p ORDER BY created_at DESC,id"
                ),
                {"p": project_id},
            ).mappings()
        ]
