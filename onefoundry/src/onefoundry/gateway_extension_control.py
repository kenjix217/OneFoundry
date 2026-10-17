"""Operator cancellation for saved extension requests and isolated jobs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.gateway_extension_calls import lock_invocation_run, public_receipt
from onefoundry.gateway_extension_spool import prepare_spool, publish_json

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def cancel_invocation(gateway: ToolGateway, project_id: UUID, invocation_id: UUID) -> dict[str, Any]:
    with gateway.engine.begin() as connection:
        gateway._require_project(connection, project_id)
        lock_invocation_run(connection, project_id, invocation_id)
        row = (
            connection.execute(
                text("SELECT * FROM extension_invocations WHERE project_id=:p AND id=:id FOR UPDATE"),
                {"p": project_id, "id": invocation_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("extension invocation not found")
        saved = dict(row)
        if saved["status"] == "cancelled":
            return public_receipt(saved)
        if saved["status"] not in {"approval_required", "ready", "running", "waiting"}:
            raise Conflict("this invocation has a terminal receipt; inspect its outcome")
        if saved["status"] == "running":
            settings = gateway.live_text_settings
            if settings is None:
                raise Conflict("extension settings are unavailable")
            root = prepare_spool(settings)
            if not (root / "jobs" / f"{invocation_id}.json").exists():
                raise Conflict("an in-flight remote effect cannot be cancelled; await its exact receipt")
            cancellation = root / "cancellations" / f"{invocation_id}.json"
            if not cancellation.exists():
                publish_json(cancellation, {"schema_version": 1, "invocation_id": str(invocation_id)})
            saved["reason"] = "cancellation_requested"
            connection.execute(
                text(
                    "UPDATE extension_invocations SET reason='cancellation_requested' "
                    "WHERE project_id=:p AND id=:id"
                ),
                {"p": project_id, "id": invocation_id},
            )
        else:
            connection.execute(
                text(
                    "UPDATE extension_invocations SET status='cancelled',reason='operator_cancelled',"
                    "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id"
                ),
                {"p": project_id, "id": invocation_id},
            )
            if saved.get("waiting_invocation_id"):
                connection.execute(
                    text(
                        "UPDATE extension_invocations SET status='cancelled',reason='parent_cancelled',"
                        "finished_at=clock_timestamp() WHERE project_id=:p AND id=:id "
                        "AND status='approval_required'"
                    ),
                    {"p": project_id, "id": saved["waiting_invocation_id"]},
                )
            saved.update(status="cancelled", reason="operator_cancelled")
        order_id = connection.scalar(
            text("SELECT work_order_id FROM runs WHERE project_id=:p AND id=:run"),
            {"p": project_id, "run": saved["run_id"]},
        )
        gateway._event(
            connection,
            project_id,
            order_id,
            saved["run_id"],
            "extension_invocation_cancel_requested",
            {"invocation_id": str(invocation_id), "status": saved["status"]},
        )
    return public_receipt(saved)
