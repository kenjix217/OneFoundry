"""One-shot SMTP submission after a committed exact operator approval."""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.smtp_accounts import SmtpAccounts
from onefoundry.smtp_transport import SmtpConfig, submit

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _finish(gateway: ToolGateway, project_id: UUID, email_id: UUID, state: str, reason: str | None) -> None:
    with gateway.engine.begin() as connection:
        updated = connection.execute(
            text(
                "UPDATE outbound_emails SET state=:s,reason_code=:reason,finished_at=clock_timestamp() "
                "WHERE project_id=:p AND id=:id AND state='dispatching' RETURNING work_order_id,run_id"
            ),
            {"s": state, "reason": reason, "p": project_id, "id": email_id},
        )
        saved = updated.mappings().one_or_none()
        if saved is not None:
            gateway._event(
                connection,
                project_id,
                saved["work_order_id"],
                saved["run_id"],
                f"outbound_email_{state}",
                {"email_id": str(email_id), "reason_code": reason},
            )


def dispatch_once(
    gateway: ToolGateway,
    accounts: SmtpAccounts,
    project_id: UUID,
    email_id: UUID,
    sender: Callable[[SmtpConfig, str, str, str, str, str], None] = submit,
) -> None:
    """Commit the claim before network I/O; an ambiguous result stays unknown."""
    with gateway.engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT e.*,a.from_address,a.enabled,a.version AS current_version FROM outbound_emails e "
                    "JOIN smtp_accounts a ON a.project_id=e.project_id AND a.id=e.account_id "
                    "JOIN approval_requests p ON p.project_id=e.project_id AND p.id=e.approval_id "
                    "WHERE e.project_id=:p AND e.id=:id AND p.status='approved' FOR UPDATE OF e,a"
                ),
                {"p": project_id, "id": email_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("approved email not found")
        if row["state"] != "approved":
            raise Conflict("email was already dispatched or is not approved")
        if not row["enabled"] or row["account_version"] != row["current_version"]:
            connection.execute(
                text(
                    "UPDATE outbound_emails SET state='failed',reason_code='account_changed',"
                    "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id"
                ),
                {"p": project_id, "id": email_id},
            )
            gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                row["run_id"],
                "outbound_email_failed",
                {"email_id": str(email_id), "reason_code": "account_changed"},
            )
            return
        account = accounts._row(connection, project_id, row["account_id"])
        connection.execute(
            text(
                "UPDATE outbound_emails SET state='dispatching',attempted_at=clock_timestamp() "
                "WHERE project_id=:p AND id=:id"
            ),
            {"p": project_id, "id": email_id},
        )
        gateway._event(
            connection,
            project_id,
            row["work_order_id"],
            row["run_id"],
            "outbound_email_dispatching",
            {"email_id": str(email_id)},
        )
    try:
        config = accounts.config(account)
    except Unavailable:
        _finish(gateway, project_id, email_id, "failed", "credential_unavailable")
        return
    try:
        sender(
            config,
            row["from_address"],
            row["to_address"],
            row["subject"],
            row["body"],
            f"<{email_id}@onefoundry.local>",
        )
    except smtplib.SMTPAuthenticationError:
        _finish(gateway, project_id, email_id, "unknown", "authentication_failed")
    except ssl.SSLError:
        _finish(gateway, project_id, email_id, "unknown", "tls_failed")
    except (OSError, smtplib.SMTPException, ValueError):
        _finish(gateway, project_id, email_id, "unknown", "smtp_result_unknown")
    else:
        _finish(gateway, project_id, email_id, "accepted", None)
