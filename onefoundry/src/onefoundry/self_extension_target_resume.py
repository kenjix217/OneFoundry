"""Resume a settled Chief turn after its latest maintenance-scope decision."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def step(gateway: ToolGateway) -> bool:
    from onefoundry.self_extension_continuation import wake

    with gateway.engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT e.project_id,s.id AS session_id,a.id AS approval_id,a.status "
                    "FROM events e JOIN approval_requests a ON a.project_id=e.project_id "
                    "AND a.id::text=e.payload->>'approval_id' JOIN task_sessions s "
                    "ON s.project_id=e.project_id AND s.work_order_id=e.work_order_id "
                    "WHERE e.event_type='self_extension_target_proposed' "
                    "AND a.status IN ('approved','rejected') AND s.status<>'closed' "
                    "AND NOT EXISTS (SELECT 1 FROM events newer WHERE newer.project_id=e.project_id "
                    "AND newer.work_order_id=e.work_order_id "
                    "AND newer.event_type='self_extension_target_proposed' "
                    "AND (newer.created_at,newer.id)>(e.created_at,e.id)) "
                    "AND NOT EXISTS (SELECT 1 FROM events requested "
                    "WHERE requested.project_id=e.project_id "
                    "AND requested.event_type='self_extension_requested' "
                    "AND requested.payload->>'target_approval_id'=a.id::text) "
                    "AND NOT EXISTS (SELECT 1 FROM events done WHERE done.project_id=e.project_id "
                    "AND done.event_type='self_extension_followup_queued' "
                    "AND done.payload->>'cause_id'=a.id::text) ORDER BY e.created_at,e.id LIMIT 8"
                )
            )
            .mappings()
            .all()
        )
    for row in rows:

        def applicable(
            connection: Any, project: UUID = row["project_id"], approval: UUID = row["approval_id"]
        ) -> bool:
            return (
                connection.scalar(
                    text(
                        "SELECT 1 FROM events e WHERE e.project_id=:p "
                        "AND e.event_type='self_extension_target_proposed' "
                        "AND e.payload->>'approval_id'=:a "
                        "AND NOT EXISTS (SELECT 1 FROM events newer WHERE newer.project_id=e.project_id "
                        "AND newer.work_order_id=e.work_order_id "
                        "AND newer.event_type='self_extension_target_proposed' "
                        "AND (newer.created_at,newer.id)>(e.created_at,e.id)) "
                        "AND NOT EXISTS (SELECT 1 FROM events requested "
                        "WHERE requested.project_id=e.project_id "
                        "AND requested.event_type='self_extension_requested' "
                        "AND requested.payload->>'target_approval_id'=:a)"
                    ),
                    {"p": project, "a": str(approval)},
                )
                is not None
            )

        if wake(
            gateway,
            row["project_id"],
            row["session_id"],
            UUID(str(row["approval_id"])),
            f"The operator {row['status']} maintenance target {row['approval_id']}. "
            "Inspect the saved decision. An approved target permits one bounded extension request; "
            "a rejected target must not be dispatched. Continue the original task from its saved checkpoint.",
            still_applicable=applicable,
        ):
            return True
    return False
