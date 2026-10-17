"""Read-only project overview and pageable order inventory for the canonical Chief."""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from onefoundry.chief_inspection_pages import InspectionPageArgs, anchor_page, inspection_page, record_page
from onefoundry.gateway_errors import NotFound
from onefoundry.project_plan import read_plan


def project_status(
    connection: Connection,
    project_id: UUID,
    arguments: InspectionPageArgs,
    max_items: int,
    byte_budget: int,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    arguments = anchor_page(connection, arguments)
    project = (
        connection.execute(
            text("SELECT id,name,goal,desired_outcome,status,version FROM projects WHERE id=:p"),
            {"p": project_id},
        )
        .mappings()
        .one_or_none()
    )
    if project is None:
        raise NotFound("project not found")
    counts = (
        connection.execute(
            text("SELECT status,count(*) AS count FROM work_orders WHERE project_id=:p GROUP BY status"),
            {"p": project_id},
        )
        .mappings()
        .all()
    )
    orders, next_offset = record_page(
        connection,
        "SELECT w.id,w.title,w.status,w.version,w.parent_work_order_id,w.profile_id,"
        "s.id AS session_id,s.status AS session_status,"
        "r.id AS latest_run_id,r.status AS latest_run_status "
        "FROM work_orders w LEFT JOIN task_sessions s ON s.project_id=w.project_id "
        "AND s.work_order_id=w.id AND s.is_order_owner "
        "LEFT JOIN LATERAL (SELECT id,status FROM runs WHERE project_id=w.project_id "
        "AND work_order_id=w.id ORDER BY started_at DESC,id DESC LIMIT 1) r ON true "
        "WHERE w.project_id=:p AND w.created_at<:created_before ORDER BY w.created_at,w.id",
        {"p": project_id},
        arguments,
        max_items,
    )
    return inspection_page(
        {
            "project": dict(project),
            "plan": read_plan(connection, project_id),
            "order_counts": {row["status"]: row["count"] for row in counts},
            "work_orders": orders,
            "capabilities": capabilities,
        },
        arguments,
        byte_budget,
        next_offset,
    )
