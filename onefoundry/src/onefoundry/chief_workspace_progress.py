"""Read-only progress projection for one saved Chief conversation root."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.chief_workspace_controls import root_order

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _first(connection: Any, statement: str, project_id: UUID, order_id: UUID) -> dict[str, Any] | None:
    row = connection.execute(text(statement), {"p": project_id, "o": order_id}).mappings().first()
    return dict(row) if row is not None else None


def progress(gateway: "ToolGateway", project_id: UUID, order_id: UUID) -> dict[str, Any]:
    """Use existing attempt, assignment, approval and artifact owners as the sole state."""
    with gateway.engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        order = root_order(connection, project_id, order_id)
        paused = bool(
            connection.scalar(
                text(
                    "SELECT paused FROM chief_conversation_controls WHERE project_id=:p AND work_order_id=:o"
                ),
                {"p": project_id, "o": order_id},
            )
        )
        counts = dict(
            connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM task_session_attempts WHERE project_id=:p AND "
                    "root_work_order_id=:o AND status='running') + "
                    "(SELECT count(*) FROM external_code_attempts a JOIN runs r "
                    "ON r.project_id=a.project_id AND r.id=a.run_id JOIN task_sessions s "
                    "ON s.project_id=r.project_id AND s.work_order_id=r.work_order_id "
                    "AND s.is_order_owner WHERE a.project_id=:p AND s.root_work_order_id=:o "
                    "AND a.status='running') AS active_attempts,"
                    "(SELECT count(*) FROM task_session_attempts WHERE project_id=:p AND "
                    "root_work_order_id=:o AND status='queued') + "
                    "(SELECT count(*) FROM external_code_attempts a JOIN runs r "
                    "ON r.project_id=a.project_id AND r.id=a.run_id JOIN task_sessions s "
                    "ON s.project_id=r.project_id AND s.work_order_id=r.work_order_id "
                    "AND s.is_order_owner WHERE a.project_id=:p AND s.root_work_order_id=:o "
                    "AND a.status='queued') AS queued_attempts,"
                    "(SELECT count(*) FROM task_session_assignments WHERE project_id=:p AND "
                    "root_work_order_id=:o AND status NOT IN ('satisfied','canceled')) "
                    "AS unfinished_assignments,"
                    "(SELECT count(*) FROM task_session_assignments WHERE project_id=:p AND "
                    "root_work_order_id=:o AND status='blocked') AS blocked_assignments,"
                    "(SELECT count(*) FROM workspace_artifacts x JOIN task_session_attempts a "
                    "ON a.project_id=x.project_id AND a.id=x.run_id WHERE x.project_id=:p "
                    "AND a.root_work_order_id=:o AND x.status='ready') AS ready_artifacts"
                ),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one()
        )

        # The exact child stays actionable even after its session closes. An acknowledgement
        # event resolves only that attempt; it does not erase its persisted history.
        unknown = _first(
            connection,
            "SELECT a.id AS attempt_id,a.session_id FROM task_session_attempts a "
            "WHERE a.project_id=:p AND a.root_work_order_id=:o AND a.status='unknown' "
            "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.project_id=a.project_id "
            "AND e.work_order_id=a.root_work_order_id "
            "AND e.event_type IN ('task_session_unknown_acknowledged',"
            "'task_session_terminal_attempt_reconciled') "
            "AND e.payload->>'attempt_id'=a.id::text) "
            "ORDER BY a.created_at,a.id LIMIT 1",
            project_id,
            order_id,
        )
        external_unknown = unknown is None
        if unknown is None:
            unknown = _first(
                connection,
                "SELECT a.id AS attempt_id,s.id AS session_id FROM external_code_attempts a "
                "JOIN runs r ON r.project_id=a.project_id AND r.id=a.run_id "
                "JOIN task_sessions s ON s.project_id=r.project_id AND s.work_order_id=r.work_order_id "
                "AND s.is_order_owner WHERE a.project_id=:p AND s.root_work_order_id=:o "
                "AND a.fixture_id='repository_code_v1' AND a.status='unknown' "
                "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.project_id=a.project_id "
                "AND e.event_type='repository_code_abandoned' "
                "AND e.payload->>'attempt_id'=a.id::text "
                "AND e.payload->>'request_sha256'=a.request_sha256 "
                "AND e.payload->>'receipt_sha256'=a.receipt_sha256) "
                "ORDER BY a.created_at,a.id LIMIT 1",
                project_id,
                order_id,
            )
        started = _first(
            connection,
            "SELECT t.attempt_id,t.session_id FROM task_session_tool_calls t "
            "JOIN task_sessions s ON s.project_id=t.project_id AND s.id=t.session_id "
            "JOIN task_session_attempts a ON a.project_id=t.project_id AND a.id=t.attempt_id "
            "WHERE t.project_id=:p AND s.root_work_order_id=:o AND t.status='started' "
            "AND a.status<>'running' "
            "ORDER BY t.created_at,t.id LIMIT 1",
            project_id,
            order_id,
        )
        approval = _first(
            connection,
            "WITH RECURSIVE descendants AS (SELECT id FROM work_orders WHERE project_id=:p AND id=:o "
            "UNION ALL SELECT w.id FROM work_orders w JOIN descendants d ON w.parent_work_order_id=d.id "
            "WHERE w.project_id=:p) "
            "SELECT a.id FROM approval_requests a JOIN descendants d ON d.id=a.work_order_id "
            "WHERE a.project_id=:p AND a.status='pending' ORDER BY a.created_at,a.id LIMIT 1",
            project_id,
            order_id,
        )
        blocked = _first(
            connection,
            "SELECT child_session_id AS session_id FROM task_session_assignments "
            "WHERE project_id=:p AND root_work_order_id=:o AND status='blocked' "
            "ORDER BY created_at,id LIMIT 1",
            project_id,
            order_id,
        )
        unfinished = _first(
            connection,
            "SELECT child_session_id AS session_id FROM task_session_assignments "
            "WHERE project_id=:p AND root_work_order_id=:o "
            "AND status NOT IN ('satisfied','canceled','blocked') "
            "ORDER BY created_at,id LIMIT 1",
            project_id,
            order_id,
        )

        blocker: dict[str, Any] | None = None
        action: dict[str, Any]
        if started is not None:
            blocker = {
                "kind": "started_action",
                "detail": "A saved action needs reconciliation.",
                "session_id": started["session_id"],
                "attempt_id": started["attempt_id"],
            }
            action = {
                "kind": "open_agent",
                "label": "Review saved action",
                "session_id": started["session_id"],
            }
        elif unknown is not None:
            blocker = {
                "kind": "unknown_attempt",
                "detail": "An attempt has an unverified outcome.",
                "session_id": unknown["session_id"],
                "attempt_id": unknown["attempt_id"],
            }
            action = (
                {"kind": "message_chief", "label": "Review coding outcome"}
                if external_unknown
                else {
                    "kind": "open_agent",
                    "label": "Review uncertain attempt",
                    "session_id": unknown["session_id"],
                }
            )
        elif approval is not None:
            blocker = {"kind": "pending_approval", "detail": "An operator decision is pending."}
            action = {"kind": "review_approval", "label": "Review approval"}
        elif blocked is not None:
            blocker = {
                "kind": "blocked_assignment",
                "detail": "A delegated assignment is blocked.",
                "session_id": blocked["session_id"],
            }
            action = {
                "kind": "open_agent",
                "label": "Review blocked agent",
                "session_id": blocked["session_id"],
            }
        elif counts["active_attempts"] or counts["queued_attempts"]:
            action = {"kind": "wait", "label": "Work is in progress"}
        elif unfinished is not None:
            blocker = {
                "kind": "unfinished_assignment",
                "detail": "A delegated assignment is unfinished.",
                "session_id": unfinished["session_id"],
            }
            action = {
                "kind": "open_agent",
                "label": "Review unfinished agent",
                "session_id": unfinished["session_id"],
            }
        elif counts["ready_artifacts"]:
            action = {"kind": "review_deliverables", "label": "Review saved deliverables"}
        elif order["status"] == "needs_review":
            action = {"kind": "review_outcome", "label": "Review completed result"}
        elif order["status"] == "done":
            action = {"kind": "none", "label": "No pending action"}
        else:
            blocker = {"kind": "awaiting_chief", "detail": "No work is queued for this conversation."}
            action = {"kind": "message_chief", "label": "Message Chief"}
        return {"counts": counts, "blocker": blocker, "next_action": action, "paused": paused}
