"""Settle parent computation when a callback approval is rejected."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_extension_calls import lock_invocation_run

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


_CALLBACK_ID = re.compile(r"extension_cb_([0-9a-f]{32})_([1-9]|1[0-6])\Z")


def reject_callback_approval(gateway: ToolGateway, project_id: UUID, approval_id: UUID) -> None:
    """Run after the approval transaction to retain run -> parent -> child lock order."""
    with gateway.engine.connect() as connection:
        child = (
            connection.execute(
                text(
                    "SELECT id,run_id,profile_id,call_id FROM extension_invocations "
                    "WHERE project_id=:p AND approval_id=:approval"
                ),
                {"p": project_id, "approval": approval_id},
            )
            .mappings()
            .one_or_none()
        )
    if child is None:
        return
    callback = _CALLBACK_ID.fullmatch(child["call_id"])
    if callback is None:
        return
    parent_id = UUID(hex=callback[1])
    with gateway.engine.begin() as connection:
        lock_invocation_run(connection, project_id, parent_id)
        parent = (
            connection.execute(
                text(
                    "SELECT id,run_id,profile_id,actor_id,status,reason,waiting_invocation_id "
                    "FROM extension_invocations WHERE project_id=:p AND id=:id FOR UPDATE"
                ),
                {"p": project_id, "id": parent_id},
            )
            .mappings()
            .one_or_none()
        )
        nested = (
            connection.execute(
                text(
                    "SELECT id,run_id,profile_id,status FROM extension_invocations "
                    "WHERE project_id=:p AND id=:id AND approval_id=:approval FOR UPDATE"
                ),
                {"p": project_id, "id": child["id"], "approval": approval_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            parent is None
            or nested is None
            or parent["run_id"] != nested["run_id"]
            or parent["profile_id"] != nested["profile_id"]
            or (
                parent["waiting_invocation_id"] is not None
                and parent["waiting_invocation_id"] != nested["id"]
            )
        ):
            return
        cancelled = parent["reason"] == "cancellation_requested"
        parent_status = "cancelled" if cancelled else "failed"
        parent_reason = "operator_cancelled" if cancelled else "connection_approval_rejected"
        child_status = "cancelled" if cancelled else "failed"
        child_reason = "parent_cancelled" if cancelled else "operator_rejected"
        if nested["status"] == "approval_required":
            connection.execute(
                text(
                    "UPDATE extension_invocations SET status=:status,reason=:reason,"
                    "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id "
                    "AND status='approval_required'"
                ),
                {"p": project_id, "id": nested["id"], "status": child_status, "reason": child_reason},
            )
        if parent["status"] not in {"running", "waiting"}:
            return
        connection.execute(
            text(
                "UPDATE extension_invocations SET status=:status,reason=:reason,"
                "waiting_invocation_id=:child,finished_at=clock_timestamp() "
                "WHERE project_id=:p AND id=:id AND status IN ('running','waiting')"
            ),
            {
                "p": project_id,
                "id": parent_id,
                "child": nested["id"],
                "status": parent_status,
                "reason": parent_reason,
            },
        )
        order_id = connection.scalar(
            text("SELECT work_order_id FROM runs WHERE project_id=:p AND id=:run"),
            {"p": project_id, "run": parent["run_id"]},
        )
        gateway._event(
            connection,
            project_id,
            order_id,
            parent["run_id"],
            "extension_invocation_finished",
            {
                "invocation_id": str(parent_id),
                "status": parent_status,
                "reason": parent_reason,
                "waiting_invocation_id": str(nested["id"]),
            },
            actor_id=parent["actor_id"],
        )
