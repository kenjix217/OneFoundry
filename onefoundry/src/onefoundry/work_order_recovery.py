"""Operator edit of an unknown live-text task without changing its prior run."""

# WORK_ORDER_COLUMNS is a fixed SQL fragment from the Tool Gateway.
# ruff: noqa: S608

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.tool_gateway import WORK_ORDER_COLUMNS
from onefoundry.workflow_schemas import WorkOrderUpdate

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def edit_unknown_live_order(
    gateway: "ToolGateway", project_id: UUID, order_id: UUID, data: WorkOrderUpdate
) -> dict[str, Any]:
    with gateway.engine.begin() as connection:
        order = (
            connection.execute(
                text(
                    f"SELECT {WORK_ORDER_COLUMNS} FROM work_orders "
                    "WHERE project_id=:project_id AND id=:order_id FOR UPDATE"
                ),
                {"project_id": project_id, "order_id": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if order is None:
            raise NotFound("work order not found")
        if order["version"] != data.expected_version:
            raise Conflict("work order changed; refresh before editing")
        latest = (
            connection.execute(
                text(
                    "SELECT id,status,mode FROM runs WHERE project_id=:project_id "
                    "AND work_order_id=:order_id ORDER BY started_at DESC,id DESC LIMIT 1"
                ),
                {"project_id": project_id, "order_id": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            order["status"] != "unknown"
            or latest is None
            or latest["status"] != "unknown"
            or latest["mode"] != "live_text"
        ):
            raise Conflict("editing requires an unknown live-text run")
        if order["title"] == data.title and order["instructions"] == data.instructions:
            raise Conflict("work order text has not changed")
        saved = (
            connection.execute(
                text(
                    "UPDATE work_orders SET title=:title,instructions=:instructions "
                    "WHERE project_id=:project_id AND id=:order_id "
                    f"RETURNING {WORK_ORDER_COLUMNS}"
                ),
                {
                    "title": data.title,
                    "instructions": data.instructions,
                    "project_id": project_id,
                    "order_id": order_id,
                },
            )
            .mappings()
            .one()
        )
        gateway._event(
            connection,
            project_id,
            order_id,
            None,
            "work_order_edited_after_unknown",
            {"prior_run_id": str(latest["id"]), "prior_version": order["version"]},
        )
        return dict(saved)
