"""Saved workflow tree, approvals and evidence, across every nested work-order branch."""

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import Connection, text

from onefoundry.chief_inspection_pages import InspectionPageArgs, anchor_page, inspection_page, record_page
from onefoundry.gateway_errors import NotFound


class WorkOrderInspectionArgs(InspectionPageArgs):
    work_order_id: UUID
    section: Literal[
        "overview",
        "sessions",
        "assignments",
        "dependencies",
        "decisions",
        "runs",
        "artifacts",
        "approvals",
        "events",
    ] = "overview"


ORDER_TREE = (
    "WITH RECURSIVE order_tree(id) AS (SELECT id FROM work_orders WHERE project_id=:p AND id=:target "
    "UNION SELECT w.id FROM work_orders w JOIN order_tree t ON w.parent_work_order_id=t.id "
    "WHERE w.project_id=:p) "
)
QUERIES = {
    "overview": "SELECT id,title,instructions,acceptance_checks,deliverable_kind,status,version,"
    "profile_id,parent_work_order_id,created_at FROM work_orders WHERE project_id=:p "
    "AND id IN (SELECT id FROM order_tree) AND created_at<:created_before ORDER BY created_at,id",
    "sessions": "SELECT s.id,s.root_work_order_id,s.work_order_id,s.parent_session_id,s.profile_id,"
    "p.name AS agent,s.kind,s.status,s.execution_mode,s.is_order_owner,s.created_at "
    "FROM task_sessions s JOIN profiles p ON p.project_id=s.project_id AND p.id=s.profile_id "
    "WHERE s.project_id=:p AND s.root_work_order_id IN (SELECT id FROM order_tree) "
    "AND s.created_at<:created_before ORDER BY s.created_at,s.id",
    "assignments": "SELECT id,root_work_order_id,parent_session_id,child_session_id,plan_revision,"
    "objective,expected_result,acceptance_check,acceptance_kind,status,result_message_id,result_sha256,"
    "created_at FROM task_session_assignments WHERE project_id=:p "
    "AND root_work_order_id IN (SELECT id FROM order_tree) "
    "AND created_at<:created_before ORDER BY created_at,id",
    "dependencies": "SELECT assignment_id,depends_on_id,root_work_order_id FROM task_session_dependencies "
    "WHERE project_id=:p AND root_work_order_id IN (SELECT id FROM order_tree) "
    "AND created_at<:created_before ORDER BY assignment_id,depends_on_id",
    "decisions": "SELECT id,main_session_id,plan_revision,kind,detail,dispatch_attempt_id,"
    "resolved_at,handled_at,created_at FROM task_session_decision_causes WHERE project_id=:p "
    "AND root_work_order_id IN (SELECT id FROM order_tree) "
    "AND created_at<:created_before ORDER BY created_at,id",
    "runs": "SELECT r.id,r.work_order_id,r.profile_id,r.status,r.mode,r.tokens_used,r.cost_usd,"
    "r.started_at,r.finished_at,rr.id AS result_id,rr.summary AS result_summary,"
    "e.id AS coding_attempt_id,e.status AS coding_status,e.reason_code AS coding_reason,"
    "e.provider_session_id,e.branch,e.commit,e.diff_sha256,e.tests,e.receipt_sha256 "
    "FROM runs r LEFT JOIN run_results rr ON rr.project_id=r.project_id AND rr.run_id=r.id "
    "LEFT JOIN external_code_attempts e ON e.project_id=r.project_id AND e.run_id=r.id "
    "WHERE r.project_id=:p AND r.work_order_id IN (SELECT id FROM order_tree) "
    "AND r.started_at<:created_before ORDER BY r.started_at,r.id",
    "artifacts": "SELECT a.id,a.run_id,a.storage,a.kind,a.mime_type,a.sha256,a.byte_size,a.status,"
    "r.status AS run_status,a.created_at FROM ("
    "SELECT id,project_id,run_id,'workspace' AS storage,kind,mime_type,sha256,byte_size,status,created_at "
    "FROM workspace_artifacts WHERE project_id=:p UNION ALL "
    "SELECT id,project_id,run_id,'runner' AS storage,kind,NULL AS mime_type,sha256,byte_size,NULL AS status,"
    "created_at FROM runner_artifacts WHERE project_id=:p UNION ALL "
    "SELECT id,project_id,run_id,'external_code' AS storage,kind,NULL AS mime_type,sha256,byte_size,"
    "NULL AS status,created_at FROM external_code_artifacts WHERE project_id=:p) a "
    "JOIN runs r ON r.project_id=a.project_id AND r.id=a.run_id "
    "WHERE a.project_id=:p AND r.work_order_id IN (SELECT id FROM order_tree) "
    "AND a.created_at<:created_before ORDER BY a.created_at,a.id",
    "approvals": "SELECT id,work_order_id,run_id,action_type,target,content,proposal_digest,status,"
    "created_at,decided_at FROM approval_requests WHERE project_id=:p "
    "AND work_order_id IN (SELECT id FROM order_tree) AND created_at<:created_before ORDER BY created_at,id",
    "events": "SELECT id,work_order_id,run_id,event_type,created_at FROM events WHERE project_id=:p "
    "AND work_order_id IN (SELECT id FROM order_tree) AND created_at<:created_before ORDER BY created_at,id",
}


def work_order_records(
    connection: Connection,
    project_id: UUID,
    arguments: WorkOrderInspectionArgs,
    max_items: int,
    byte_budget: int,
) -> dict[str, Any]:
    arguments = anchor_page(connection, arguments)
    if (
        connection.scalar(
            text("SELECT 1 FROM work_orders WHERE project_id=:p AND id=:target"),
            {"p": project_id, "target": arguments.work_order_id},
        )
        is None
    ):
        raise NotFound("work order not found in this project")
    rows, next_offset = record_page(
        connection,
        ORDER_TREE + QUERIES[arguments.section],
        {"p": project_id, "target": arguments.work_order_id},
        arguments,
        max_items,
    )
    return inspection_page(
        {"work_order_id": str(arguments.work_order_id), "section": arguments.section, "records": rows},
        arguments,
        byte_budget,
        next_offset,
    )
