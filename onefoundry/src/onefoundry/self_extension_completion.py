"""Completion gate for exact self-extension checkpoints and verified replacements."""

from typing import Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict


def require_resumed(connection: Any, project_id: UUID, root_id: UUID) -> None:
    """A later verified replacement may retire completed or canceled work that never activated.

    Successful coding alone does not authorize installation. Every proposed
    activation must be rejected and no activation may have been queued before
    its completed or canceled candidate can be superseded. Unknown and live attempts retain
    their existing blockers; historical events and receipts are never rewritten.
    """
    waiting = connection.scalar(
        text(
            "WITH retired AS (SELECT request.id FROM events request "
            "WHERE request.project_id=:p AND request.event_type='self_extension_requested' "
            "AND request.payload->>'original_work_order_id'=:root "
            "AND EXISTS (SELECT 1 FROM task_session_assignments assignment "
            "WHERE assignment.project_id=request.project_id "
            "AND assignment.root_work_order_id::text=request.payload->>'original_work_order_id' "
            "AND assignment.child_session_id::text=request.payload->>'child_session_id' "
            "AND assignment.status IN ('canceled','satisfied')) "
            "AND NOT EXISTS (SELECT 1 FROM events queued WHERE queued.project_id=request.project_id "
            "AND queued.event_type='self_extension_activation_queued' "
            "AND queued.payload->'manifest'->>'target_approval_id'=request.payload->>'target_approval_id') "
            "AND NOT EXISTS (SELECT 1 FROM events proposed JOIN approval_requests approval "
            "ON approval.project_id=proposed.project_id "
            "AND approval.id::text=proposed.payload->>'approval_id' "
            "WHERE proposed.project_id=request.project_id "
            "AND proposed.event_type='self_extension_activation_proposed' "
            "AND approval.status<>'rejected' "
            "AND (CASE WHEN proposed.event_type='self_extension_activation_proposed' "
            "THEN approval.content::jsonb ELSE '{}'::jsonb END)->>'target_approval_id' "
            "=request.payload->>'target_approval_id')) "
            "SELECT 1 FROM events request WHERE request.project_id=:p "
            "AND request.event_type='self_extension_requested' "
            "AND request.payload->>'original_work_order_id'=:root AND NOT EXISTS "
            "(SELECT 1 FROM events resumed WHERE resumed.project_id=request.project_id "
            "AND resumed.event_type='self_extension_resumed' AND resumed.payload->>"
            "'original_session_id'=request.payload->>'original_session_id' "
            "AND resumed.payload->>'extension_key'=request.payload->>'extension_key') "
            "AND NOT (EXISTS (SELECT 1 FROM events replacement JOIN events resumed "
            "ON resumed.project_id=replacement.project_id AND resumed.event_type='self_extension_resumed' "
            "AND resumed.payload->>'extension_key'=replacement.payload->>'extension_key' "
            "AND resumed.payload->>'original_session_id'=replacement.payload->>'original_session_id' "
            "WHERE replacement.project_id=request.project_id "
            "AND replacement.event_type='self_extension_requested' "
            "AND (replacement.created_at,replacement.id)>(request.created_at,request.id) "
            "AND replacement.payload->>'original_session_id'=request.payload->>'original_session_id' "
            "AND replacement.payload->>'original_work_order_id'=request.payload->>'original_work_order_id' "
            "AND replacement.payload->>'operation'=request.payload->>'operation' "
            "AND replacement.payload->>'gap_source_id' IS NOT DISTINCT FROM "
            "request.payload->>'gap_source_id' "
            "AND replacement.payload->>'gap_revision' IS NOT DISTINCT FROM request.payload->>'gap_revision') "
            "AND (request.id IN (SELECT id FROM retired) OR EXISTS "
            "(SELECT 1 FROM task_sessions child JOIN runs r "
            "ON r.project_id=child.project_id AND r.work_order_id=child.work_order_id "
            "JOIN external_code_attempts a ON a.project_id=r.project_id AND a.run_id=r.id "
            "WHERE child.project_id=request.project_id AND "
            "child.id::text=request.payload->>'child_session_id' "
            "AND a.fixture_id='repository_code_v1')) "
            "AND NOT EXISTS (SELECT 1 FROM task_sessions child JOIN runs r "
            "ON r.project_id=child.project_id AND r.work_order_id=child.work_order_id "
            "JOIN external_code_attempts a ON a.project_id=r.project_id AND a.run_id=r.id "
            "WHERE child.project_id=request.project_id AND "
            "child.id::text=request.payload->>'child_session_id' "
            "AND a.fixture_id='repository_code_v1' AND NOT (a.status='failed' OR "
            "(a.status='done' AND request.id IN (SELECT id FROM retired)) OR "
            "(a.status='unknown' AND EXISTS (SELECT 1 FROM events abandoned "
            "WHERE abandoned.project_id=a.project_id AND abandoned.event_type='repository_code_abandoned' "
            "AND abandoned.payload->>'attempt_id'=a.id::text "
            "AND abandoned.payload->>'request_sha256'=a.request_sha256 "
            "AND abandoned.payload->>'receipt_sha256'=a.receipt_sha256))))) LIMIT 1"
        ),
        {"p": project_id, "root": str(root_id)},
    )
    if waiting is not None:
        raise Conflict("the extension is still waiting for verified activation and original-task resumption")
