"""Compact authoritative team progress shared by Chief entrypoints."""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from onefoundry.chief_work_order_inspection import ORDER_TREE


def workflow_summary(connection: Connection, project_id: UUID, work_order_id: UUID) -> dict[str, Any]:
    parameters = {"p": project_id, "target": work_order_id}
    states = (
        connection.execute(
            text(
                ORDER_TREE + "SELECT status,count(*) AS count FROM task_session_assignments "  # noqa: S608 - static owner SQL
                "WHERE project_id=:p AND root_work_order_id IN (SELECT id FROM order_tree) GROUP BY status"
            ),
            parameters,
        )
        .mappings()
        .all()
    )
    attempts = (
        connection.execute(
            text(
                ORDER_TREE + "SELECT status,count(*) AS count FROM task_session_attempts "  # noqa: S608 - static owner SQL
                "WHERE project_id=:p AND root_work_order_id IN (SELECT id FROM order_tree) GROUP BY status"
            ),
            parameters,
        )
        .mappings()
        .all()
    )
    return {
        "assignment_counts": {row["status"]: row["count"] for row in states},
        "attempt_counts": {row["status"]: row["count"] for row in attempts},
        "evidence_scope": "all nested team branches",
        "next_step": "Inspect the work-order team's saved sessions for exact findings and blockers. "
        "A completed model turn is separate from accepted assignments and operator completion.",
    }
