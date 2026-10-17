"""Settle a known prelaunch rejection without fabricating a coding receipt."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.profile_hierarchy import lock_project
from onefoundry.task_session_decisions import TaskSessionDecisions

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def reject(gateway: "ToolGateway", project_id: UUID, intent: Any, reason: str) -> bool:
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        row = (
            connection.execute(
                text(
                    "SELECT a.id,a.child_session_id,s.work_order_id FROM task_session_assignments a "
                    "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.child_session_id "
                    "JOIN task_session_attempts t ON t.project_id=s.project_id AND t.session_id=s.id "
                    "WHERE a.project_id=:p AND s.id=:s AND s.work_order_id=:o AND s.status<>'closed' "
                    "AND a.status IN ('ready','active') AND t.id=:t AND t.status='done' "
                    "AND NOT EXISTS (SELECT 1 FROM task_session_attempts busy WHERE busy.project_id=:p "
                    "AND busy.session_id=s.id AND busy.status IN ('queued','running')) "
                    "AND NOT EXISTS (SELECT 1 FROM external_code_attempts c WHERE c.project_id=:p "
                    "AND c.idempotency_key=:key) FOR UPDATE OF a"
                ),
                {
                    "p": project_id,
                    "s": UUID(intent["session_id"]),
                    "o": UUID(intent["work_order_id"]),
                    "t": UUID(intent["source_attempt_id"]),
                    "key": UUID(intent["launch"]["idempotency_key"]),
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        saved = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p "
                "AND event_type='self_extension_build_requested' AND payload->>'tool_call_id'=:id"
            ),
            {"p": project_id, "id": intent["tool_call_id"]},
        )
        if saved != intent:
            return False
        connection.execute(
            text("UPDATE task_session_assignments SET status='blocked' WHERE project_id=:p AND id=:a"),
            {"p": project_id, "a": row["id"]},
        )
        TaskSessionDecisions(gateway).record_blocker(
            connection, project_id, row["child_session_id"], UUID(intent["source_attempt_id"]), reason
        )
        prior = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND event_type='self_extension_dispatch_blocked' "
                "AND payload->>'tool_call_id'=:id"
            ),
            {"p": project_id, "id": intent["tool_call_id"]},
        )
        observation = {
            "tool_call_id": intent["tool_call_id"],
            "assignment_id": str(row["id"]),
            "owner": "repository_code_gateway",
            "status": "needs_setup",
            "reason": reason,
        }
        if prior is None:
            gateway._event(
                connection,
                project_id,
                row["work_order_id"],
                None,
                "self_extension_dispatch_blocked",
                observation,
            )
        gateway._event(
            connection,
            project_id,
            row["work_order_id"],
            None,
            "self_extension_dispatch_rejected",
            observation,
        )
        return True
