"""Finish approved local jobs from accepted work and independent QA evidence."""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools


def policy(connection: Any, project_id: UUID, order_id: UUID) -> bool:
    return bool(
        connection.scalar(
            text(
                "SELECT completion_policy FROM chief_conversation_controls WHERE "
                "project_id=:p AND work_order_id=:o"
            ),
            {"p": project_id, "o": order_id},
        )
        == "chief_verified"
    )


def verify(connection: Any, tools: "TaskSessionTools", scope: Mapping[str, Any], action_id: UUID) -> None:
    args = {"p": tools.project_id, "o": scope["root_work_order_id"], "a": tools.attempt_id, "call": action_id}
    unsafe = connection.scalar(
        text(
            "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND "
            "root_work_order_id=:o AND id<>:a AND status IN ('queued','running') "
            "UNION ALL SELECT 1 FROM task_session_tool_calls WHERE project_id=:p AND "
            "root_work_order_id=:o AND id<>:call AND status='started' LIMIT 1"
        ),
        args,
    )
    reviews = (
        connection.execute(
            text(
                "SELECT m.body FROM task_session_assignments a "
                "JOIN task_sessions reviewer ON reviewer.project_id=a.project_id "
                "AND reviewer.id=a.child_session_id JOIN task_session_messages m "
                "ON m.project_id=a.project_id AND m.id=a.result_message_id "
                "WHERE a.project_id=:p AND a.root_work_order_id=:o AND a.acceptance_kind='qa_review' "
                "AND a.status='satisfied' AND EXISTS(SELECT 1 FROM task_session_dependencies d "
                "JOIN task_session_assignments source ON "
                "source.project_id=d.project_id AND source.id=d.depends_on_id "
                "JOIN task_sessions producer ON producer.project_id=source.project_id "
                "AND producer.id=source.child_session_id "
                "WHERE d.project_id=a.project_id AND d.assignment_id=a.id AND source.status='satisfied' "
                "AND producer.profile_id<>reviewer.profile_id "
                "AND source.acceptance_kind IN ('text_report','artifact'))"
            ),
            args,
        )
        .scalars()
        .all()
    )
    verified = False
    for body in reviews:
        try:
            review = json.loads(body)
        except (ValueError, TypeError) as exc:
            raise Conflict("saved QA evidence is malformed; request a fresh review") from exc
        if not isinstance(review, dict) or review.get("verdict") != "passed":
            raise Conflict("saved QA evidence does not confirm a passed review")
        verified = True
    if unsafe is not None:
        raise Conflict("another turn or action is still active; finish it before local completion")
    if not verified:
        raise Conflict("verified local completion requires accepted work and passed independent QA")


def close_local_children(
    gateway: Any, connection: Any, project_id: UUID, order_id: UUID, reason: str
) -> None:
    accepted = (
        connection.execute(
            text(
                "UPDATE work_orders w SET status='done' WHERE w.project_id=:p AND w.status='needs_review' "
                "AND w.deliverable_kind NOT IN "
                "('code_change','external_action','operator_review') AND w.id IN "
                "(SELECT s.work_order_id FROM task_sessions s JOIN "
                "task_session_assignments a ON a.project_id=s.project_id AND "
                "a.child_session_id=s.id "
                "WHERE a.project_id=:p AND a.root_work_order_id=:o AND a.status='satisfied') RETURNING w.id"
            ),
            {"p": project_id, "o": order_id},
        )
        .scalars()
        .all()
    )
    for child in accepted:
        gateway._event(
            connection,
            project_id,
            child,
            None,
            "work_order_accepted",
            {
                "from_status": "needs_review",
                "status": "done",
                "reason": reason,
                "team_root_work_order_id": str(order_id),
                "acceptance_owner": "chief_verified",
            },
        )
    connection.execute(
        text("UPDATE task_sessions SET status='closed' WHERE project_id=:p AND root_work_order_id=:o"),
        {"p": project_id, "o": order_id},
    )
