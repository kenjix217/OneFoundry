"""Availability and saved QA feedback for exactly scoped assignment reads."""

from typing import Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound


def scoped_assignment(
    connection: Any, project_id: UUID, session_id: UUID, assignment_id: UUID, own_assignment: Any
) -> dict[str, Any]:
    if own_assignment is not None:
        query = (
            "SELECT previous.id,previous.status,previous.acceptance_kind,previous.child_session_id "
            "FROM task_session_dependencies d JOIN task_session_assignments previous "
            "ON previous.project_id=d.project_id AND previous.id=d.depends_on_id "
            "WHERE d.project_id=:p AND d.assignment_id=:own AND previous.id=:target"
        )
        parameters = {"p": project_id, "own": own_assignment["id"], "target": assignment_id}
    else:
        query = (
            "SELECT id,status,acceptance_kind,child_session_id FROM task_session_assignments "
            "WHERE project_id=:p AND parent_session_id=:s AND id=:target"
        )
        parameters = {"p": project_id, "s": session_id, "target": assignment_id}
    row = connection.execute(text(query), parameters).mappings().one_or_none()
    if row is None:
        raise NotFound("assignment is outside this session's declared inputs")
    return dict(row)


def availability(assignment: dict[str, Any]) -> dict[str, Any]:
    state = assignment["status"]
    if state in ("pending", "ready", "active"):
        status = "pending"
        summary = "This assignment has not saved a confirmed result yet."
        next_step = (
            "Finish this turn so queued specialists can run. Do not poll again in this turn; "
            "read the result in a subsequent turn after the team records it."
        )
    elif state == "blocked":
        status = state
        summary = "This assignment is blocked; it has no accepted result."
        next_step = (
            "Use the saved blocker or QA feedback to plan rework. Resolve the cause before "
            "creating replacement work; do not treat this assignment as satisfied."
        )
    elif state == "canceled":
        status = state
        summary = "This assignment was canceled; it has no current accepted result."
        next_step = "Use the replacement assignment's confirmed result, if one was created."
    else:
        raise Conflict("assignment's confirmed result is unavailable; reconcile its saved evidence")
    return {
        "assignment_id": str(assignment["id"]),
        "assignment_status": state,
        "status": status,
        "content_available": False,
        "summary": summary,
        "next_step": next_step,
    }


def qa_feedback(connection: Any, project_id: UUID, assignment: dict[str, Any]) -> dict[str, Any] | None:
    """Return emitted review bytes tied to a completed needs-rework tool receipt."""
    if assignment["status"] != "blocked" or assignment.get("acceptance_kind") != "qa_review":
        return None
    row = (
        connection.execute(
            text(
                "SELECT m.id AS message_id,m.body FROM task_session_tool_calls t "
                "JOIN task_session_messages m ON m.project_id=t.project_id AND m.session_id=t.session_id "
                "AND m.id::text=t.result->>'message_id' "
                "WHERE t.project_id=:p AND t.session_id=:s "
                "AND t.tool_name='onefoundry.report_result' AND t.status='done' "
                "AND t.result->>'status'='needs_rework' ORDER BY t.finished_at DESC,t.id DESC LIMIT 1"
            ),
            {"p": project_id, "s": assignment["child_session_id"]},
        )
        .mappings()
        .one_or_none()
    )
    return {"assignment_id": assignment["id"], **dict(row)} if row is not None else None
