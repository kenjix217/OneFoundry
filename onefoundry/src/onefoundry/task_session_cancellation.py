"""Cancel a blocked branch before bounded rework; never interrupt uncertain effects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict
from onefoundry.profile_hierarchy import lock_project
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_uncertainty import unresolved as has_uncertainty

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools
    from onefoundry.tool_gateway import ToolGateway


def cancel(
    tools: TaskSessionTools, scope: Mapping[str, Any], assignment_id: UUID, action_id: UUID
) -> dict[str, Any]:
    with tools.gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        rows = (
            connection.execute(
                text(
                    "WITH RECURSIVE branch AS (SELECT id,child_session_id,status,plan_revision FROM "
                    "task_session_assignments "
                    "WHERE project_id=:p AND root_work_order_id=:root AND parent_session_id=:parent "
                    "AND id=:id "
                    "UNION SELECT a.id,a.child_session_id,a.status,a.plan_revision "
                    "FROM task_session_dependencies d "
                    "JOIN branch b ON b.id=d.depends_on_id JOIN task_session_assignments a "
                    "ON a.project_id=d.project_id AND a.id=d.assignment_id WHERE d.project_id=:p) "
                    "SELECT * FROM branch"
                ),
                {
                    "p": tools.project_id,
                    "root": scope["root_work_order_id"],
                    "parent": tools.session_id,
                    "id": assignment_id,
                },
            )
            .mappings()
            .all()
        )
        if not rows or any(
            item["status"] not in ("pending", "ready", "blocked", "canceled") for item in rows
        ):
            raise Conflict("only an uncompleted, inactive branch can be canceled")
        sessions = [item["child_session_id"] for item in rows]
        unsafe = connection.scalar(
            text(
                "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND session_id=ANY(:ids) "
                "AND status='running' LIMIT 1"
            ),
            {"p": tools.project_id, "ids": sessions},
        )
        unresolved = connection.scalar(
            text(
                "SELECT 1 FROM task_session_tool_calls WHERE project_id=:p AND session_id=ANY(:ids) "
                "AND status='started' LIMIT 1"
            ),
            {"p": tools.project_id, "ids": sessions},
        )
        if (
            unsafe is not None
            or unresolved is not None
            or any(has_uncertainty(connection, tools.project_id, session_id=s) for s in sessions)
        ):
            raise Conflict("an active or unknown attempt needs operator reconciliation before cancellation")
        connection.execute(
            text(
                "UPDATE task_session_attempts SET status='failed',reason='assignment_canceled',"
                "finished_at=clock_timestamp() WHERE project_id=:p AND session_id=ANY(:ids) AND "
                "status='queued'"
            ),
            {"p": tools.project_id, "ids": sessions},
        )
        connection.execute(
            text(
                "UPDATE task_session_assignments SET status='canceled' WHERE project_id=:p AND id=ANY(:ids)"
            ),
            {"p": tools.project_id, "ids": [item["id"] for item in rows]},
        )
        connection.execute(
            text("UPDATE task_sessions SET status='closed' WHERE project_id=:p AND id=ANY(:ids)"),
            {"p": tools.project_id, "ids": sessions},
        )
        connection.execute(
            text(
                "UPDATE work_orders SET status='failed' WHERE project_id=:p AND id IN "
                "(SELECT work_order_id FROM task_sessions WHERE project_id=:p AND id=ANY(:ids)) "
                "AND status IN ('draft','ready','running','blocked')"
            ),
            {"p": tools.project_id, "ids": sessions},
        )
        tools.gateway._event(
            connection,
            tools.project_id,
            scope["root_work_order_id"],
            tools.attempt_id,
            "task_session_branch_canceled",
            {"tool_call_id": str(action_id), "assignment_ids": [str(item["id"]) for item in rows]},
        )
        decisions = TaskSessionDecisions(tools.gateway)
        for revision in sorted({item["plan_revision"] for item in rows}):
            decisions.record_synthesis_if_ready(
                connection, tools.project_id, scope["root_work_order_id"], revision
            )
        return {"status": "canceled", "assignment_ids": [str(item["id"]) for item in rows]}


def cancel_order(
    gateway: ToolGateway, project_id: UUID, order_id: UUID, action_id: UUID, expected_version: int
) -> dict[str, Any]:
    """An operator can stop queued work; live or unknown effects must be reconciled."""
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND work_order_id=:o "
                "AND event_type='task_session_order_canceled' AND payload->>'client_action_id'=:a LIMIT 1"
            ),
            {"p": project_id, "o": order_id, "a": str(action_id)},
        )
        if prior is not None:
            return dict(prior)
        order = (
            connection.execute(
                text(
                    "SELECT w.version,w.status,s.id FROM work_orders w JOIN task_sessions s "
                    "ON s.project_id=w.project_id AND s.work_order_id=w.id AND s.kind='main' "
                    "AND s.execution_mode='team' WHERE w.project_id=:p AND w.id=:o FOR UPDATE OF w,s"
                ),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            order is None
            or order["version"] != expected_version
            or order["status"] not in ("ready", "running")
        ):
            raise Conflict("refresh an active team work order before canceling")
        unsafe = connection.scalar(
            text(
                "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND root_work_order_id=:o "
                "AND status='running' LIMIT 1"
            ),
            {"p": project_id, "o": order_id},
        )
        unresolved = connection.scalar(
            text(
                "SELECT 1 FROM task_session_tool_calls WHERE project_id=:p AND root_work_order_id=:o "
                "AND status='started' LIMIT 1"
            ),
            {"p": project_id, "o": order_id},
        )
        if (
            unsafe is not None
            or unresolved is not None
            or has_uncertainty(connection, project_id, root_id=order_id)
        ):
            raise Conflict("active or unknown attempts need operator reconciliation before cancellation")
        changed = connection.execute(
            text(
                "UPDATE task_session_attempts SET status='failed',reason='operator_canceled',"
                "finished_at=clock_timestamp() WHERE project_id=:p AND root_work_order_id=:o "
                "AND status='queued'"
            ),
            {"p": project_id, "o": order_id},
        )
        connection.execute(
            text(
                "UPDATE task_session_assignments SET status='canceled' WHERE project_id=:p "
                "AND root_work_order_id=:o AND status IN ('pending','ready','blocked')"
            ),
            {"p": project_id, "o": order_id},
        )
        connection.execute(
            text("UPDATE task_sessions SET status='closed' WHERE project_id=:p AND root_work_order_id=:o"),
            {"p": project_id, "o": order_id},
        )
        connection.execute(
            text(
                "UPDATE work_orders SET status='failed' WHERE project_id=:p AND id IN "
                "(SELECT work_order_id FROM task_sessions WHERE project_id=:p AND root_work_order_id=:o) "
                "AND status IN ('draft','ready','running','blocked')"
            ),
            {"p": project_id, "o": order_id},
        )
        result = {
            "client_action_id": str(action_id),
            "status": "failed",
            "reason": "operator_canceled",
            "queued_canceled": changed.rowcount,
        }
        gateway._event(connection, project_id, order_id, None, "task_session_order_canceled", result)
        return result
