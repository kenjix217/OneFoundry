"""Each operator objective appears once; delegated records remain linked history."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.agent_conversation_policy import is_direct_conversation
from onefoundry.gateway_errors import NotFound
from onefoundry.profile_hierarchy import chief_profile_id
from onefoundry.task_session_uncertainty import unresolved

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def list_conversations(gateway: "ToolGateway", project_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        if connection.scalar(text("SELECT 1 FROM projects WHERE id=:p"), {"p": project_id}) is None:
            raise NotFound("project not found")
        chief = chief_profile_id(connection, project_id)
        rows = (
            connection.execute(
                text(
                    "SELECT w.id,w.title,w.status,w.version,w.profile_id,w.created_at,w.deliverable_kind,"
                    "p.name AS profile_name,coalesce(c.archived,false) AS "
                    "archived,coalesce(c.paused,false) AS paused,"
                    "coalesce(c.trashed,false) AS trashed,"
                    "coalesce(c.version,0) AS "
                    "controls_version,coalesce(c.completion_policy,'operator_review') AS "
                    "completion_policy,"
                    "(SELECT count(*) FROM task_session_attempts a WHERE "
                    "a.project_id=w.project_id AND a.root_work_order_id=w.id AND "
                    "a.status='running' AND a.purpose='work') AS active,"
                    "(SELECT count(*) FROM task_session_attempts a WHERE "
                    "a.project_id=w.project_id AND a.root_work_order_id=w.id AND "
                    "a.status='queued' AND a.purpose='work') AS queued,"
                    "(SELECT count(*) FROM external_code_attempts a JOIN runs r "
                    "ON r.project_id=a.project_id AND r.id=a.run_id WHERE "
                    "a.project_id=w.project_id AND a.status='queued' AND EXISTS "
                    "(SELECT 1 FROM task_sessions s WHERE s.project_id=r.project_id "
                    "AND s.work_order_id=r.work_order_id AND s.root_work_order_id=w.id)) "
                    "AS external_active,"
                    "(SELECT a.status FROM task_session_attempts a JOIN task_sessions s ON "
                    "s.project_id=a.project_id AND s.id=a.session_id WHERE a.project_id=w.project_id "
                    "AND a.root_work_order_id=w.id AND s.kind='main' AND a.purpose='work' "
                    "ORDER BY a.created_at DESC,a.id DESC LIMIT 1) AS latest_work_outcome,"
                    "(SELECT count(*) FROM task_session_assignments a WHERE "
                    "a.project_id=w.project_id AND a.root_work_order_id=w.id AND "
                    "a.status='blocked') AS blocked_assignments,"
                    "(SELECT e.payload->'receipt' FROM events e WHERE e.project_id=w.project_id "
                    "AND e.work_order_id=w.id AND e.event_type='chief_workspace_message' "
                    "ORDER BY e.created_at DESC,e.id DESC LIMIT 1) AS latest_receipt,"
                    "(SELECT max(m.created_at) FROM task_session_messages m WHERE "
                    "m.project_id=w.project_id AND m.root_work_order_id=w.id) AS updated_at "
                    "FROM work_orders w JOIN profiles p ON p.project_id=w.project_id AND p.id=w.profile_id "
                    "LEFT JOIN chief_conversation_controls c ON c.project_id=w.project_id "
                    "AND c.work_order_id=w.id "
                    "WHERE w.project_id=:p AND w.parent_work_order_id IS NULL ORDER BY coalesce("
                    "(SELECT max(m.created_at) FROM task_session_messages m WHERE "
                    "m.project_id=w.project_id AND m.root_work_order_id=w.id),w.created_at) "
                    "DESC,w.id"
                ),
                {"p": project_id},
            )
            .mappings()
            .all()
        )
        result = []
        for row in rows:
            item = dict(row)
            item["canonical_chief"] = row["profile_id"] == chief
            item["direct_agent"] = row["profile_id"] != chief and is_direct_conversation(
                connection, project_id, row["id"], row["profile_id"]
            )
            unknown = unresolved(connection, project_id, root_id=row["id"])
            receipt = row["latest_receipt"] or {}
            item["state"] = (
                "Pausing"
                if row["paused"] and (row["active"] or row["external_active"])
                else "Paused"
                if row["paused"]
                else "Checking outcome"
                if unknown or row["status"] == "unknown"
                else "Working"
                if row["active"] or row["external_active"]
                else "Queued"
                if row["queued"]
                else "Completed"
                if row["status"] == "done"
                else "Review requested"
                if row["status"] == "needs_review"
                else "Stopped"
                if row["status"] in ("failed", "blocked")
                or row["latest_work_outcome"] == "failed"
                or row["blocked_assignments"]
                else "Setup needed"
                if receipt.get("disposition") == "awaiting_setup"
                else "Not started"
                if row["status"] in ("draft", "ready")
                else "Waiting for direction"
            )
            item["detail"] = (
                "An earlier outcome is unverified. The agent can discuss its saved context here; "
                "work effects remain held."
                if unknown
                else receipt.get("detail")
                if item["state"] == "Setup needed"
                else (
                    "Work stopped. Message Chief here to review the saved work and continue."
                    if row["profile_id"] == chief
                    else f"Work stopped. Message {row['profile_name']} here to review "
                    "the saved work and continue."
                )
                if item["state"] == "Stopped"
                else None
            )
            item.pop("latest_receipt")
            item.pop("external_active")
            result.append(item)
        return result


def resolve_conversation(gateway: "ToolGateway", project_id: UUID, order_id: UUID) -> dict[str, Any]:
    with gateway.engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        row = (
            connection.execute(
                text(
                    "WITH RECURSIVE ancestry AS (SELECT id,parent_work_order_id FROM "
                    "work_orders WHERE project_id=:p AND id=:o "
                    "UNION ALL SELECT w.id,w.parent_work_order_id FROM work_orders w JOIN "
                    "ancestry a ON a.parent_work_order_id=w.id WHERE w.project_id=:p) "
                    "SELECT a.id AS order_id,(SELECT s.id FROM task_sessions s WHERE "
                    "s.project_id=:p AND s.work_order_id=:o AND s.is_order_owner) AS "
                    "agent_session_id "
                    "FROM ancestry a WHERE a.parent_work_order_id IS NULL"
                ),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or connection.scalar(
            text("SELECT trashed FROM chief_conversation_controls WHERE project_id=:p AND work_order_id=:o"),
            {"p": project_id, "o": row["order_id"]},
        ):
            raise NotFound("conversation not found in this project")
        return dict(row)
