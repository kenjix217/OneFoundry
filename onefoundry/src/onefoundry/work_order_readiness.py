"""Admit an existing draft through the Gateway's normal state transition."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def mark_ready(gateway: ToolGateway, connection: Any, project_id: UUID, order_id: UUID) -> dict[str, Any]:
    from onefoundry.tool_gateway import WORK_ORDER_COLUMNS

    status = connection.scalar(
        text("SELECT status FROM work_orders WHERE project_id=:p AND id=:o FOR UPDATE"),
        {"p": project_id, "o": order_id},
    )
    if status is None:
        raise NotFound("work order not found")
    if status != "draft":
        raise Conflict("work order is not draft")
    row = (
        connection.execute(
            text(  # Only the owner's static column list is interpolated; request values are bound.
                "UPDATE work_orders SET status='ready' WHERE project_id=:p AND id=:o "  # noqa: S608
                f"RETURNING {WORK_ORDER_COLUMNS}"
            ),
            {"p": project_id, "o": order_id},
        )
        .mappings()
        .one()
    )
    gateway._event(connection, project_id, order_id, None, "work_order_ready", {"status": "ready"})
    return dict(row)
