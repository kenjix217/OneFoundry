"""Keep operator review of a completed team aligned with its child work orders."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict
from onefoundry.task_session_store import TaskSessionStore
from onefoundry.task_session_uncertainty import unresolved as has_uncertainty

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def review(
    gateway: ToolGateway, connection: Any, project_id: UUID, order_id: UUID, action: str, reason: str
) -> None:
    session = connection.execute(
        text(
            "SELECT id FROM task_sessions WHERE project_id=:p AND work_order_id=:o "
            "AND root_work_order_id=:o AND kind='main' AND execution_mode='team'"
        ),
        {"p": project_id, "o": order_id},
    ).scalar()
    if session is None or action not in ("accept", "return"):
        return
    unsafe = connection.scalar(
        text(
            "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND root_work_order_id=:o "
            "AND status IN ('queued','running') LIMIT 1"
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
    pending = connection.scalar(
        text(
            "SELECT 1 FROM approval_requests a JOIN task_sessions s ON s.project_id=a.project_id "
            "AND s.work_order_id=a.work_order_id WHERE a.project_id=:p AND s.root_work_order_id=:o "
            "AND a.status='pending' LIMIT 1"
        ),
        {"p": project_id, "o": order_id},
    )
    if (
        unsafe is not None
        or unresolved is not None
        or pending is not None
        or has_uncertainty(connection, project_id, root_id=order_id)
    ):
        raise Conflict("team execution or action evidence needs reconciliation before review")
    if action == "return":
        sequence = TaskSessionStore._sequence(connection, project_id, session) + 1
        connection.execute(
            text(
                "INSERT INTO task_session_messages (id,project_id,actor_id,root_work_order_id,"
                "session_id,sequence,kind,body) VALUES (:id,:p,:actor,:o,:s,:seq,'operator',:body)"
            ),
            {
                "id": uuid4(),
                "p": project_id,
                "actor": gateway.actor_id,
                "o": order_id,
                "s": session,
                "seq": sequence,
                "body": f"Operator returned the deliverable for rework: {reason}",
            },
        )
        return
    incomplete = connection.scalar(
        text(
            "SELECT 1 FROM task_session_assignments WHERE project_id=:p AND root_work_order_id=:o "
            "AND status NOT IN ('satisfied','canceled') LIMIT 1"
        ),
        {"p": project_id, "o": order_id},
    )
    submitted = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:p AND work_order_id=:o "
            "AND event_type='task_session_completion_submitted' LIMIT 1"
        ),
        {"p": project_id, "o": order_id},
    )
    if incomplete is not None or submitted is None:
        raise Conflict("team results have not been submitted for acceptance")
    accepted = (
        connection.execute(
            text(
                "UPDATE work_orders w SET status='done' WHERE w.project_id=:p AND w.status='needs_review' "
                "AND w.deliverable_kind NOT IN ('code_change','external_action') AND w.id IN "
                "(SELECT s.work_order_id FROM task_sessions s JOIN task_session_assignments a "
                "ON a.project_id=s.project_id AND a.child_session_id=s.id WHERE a.project_id=:p "
                "AND a.root_work_order_id=:o AND a.status='satisfied') RETURNING w.id"
            ),
            {"p": project_id, "o": order_id},
        )
        .scalars()
        .all()
    )
    for child_order_id in accepted:
        gateway._event(
            connection,
            project_id,
            child_order_id,
            None,
            "work_order_accepted",
            {
                "from_status": "needs_review",
                "status": "done",
                "reason": reason,
                "team_root_work_order_id": str(order_id),
            },
        )
    connection.execute(
        text("UPDATE task_sessions SET status='closed' WHERE project_id=:p AND root_work_order_id=:o"),
        {"p": project_id, "o": order_id},
    )
