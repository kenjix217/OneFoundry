"""Read-only next actions projected from organization, workflow, evidence and approval owners."""

from typing import Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.onefoundry_next_action import OneFoundryActionSnapshot, next_action


def inbox(connection: Any, project_id: UUID) -> dict[str, Any]:
    rows = connection.execute(
        text(
            "SELECT o.id,o.display_name,o.qualification,w.status AS workflow_status,"
            "w.revision AS workflow_revision,w.blocked_reason,"
            "(SELECT s.revision FROM onefoundry_sources s WHERE s.project_id=o.project_id "
            "AND s.organization_id=o.id AND s.superseded_by IS NULL "
            "ORDER BY s.created_at DESC,s.id DESC LIMIT 1) AS source_revision,"
            "EXISTS(SELECT 1 FROM onefoundry_sources s WHERE s.project_id=o.project_id "
            "AND s.organization_id=o.id) AS has_source,"
            "EXISTS(SELECT 1 FROM onefoundry_claims c JOIN onefoundry_sources s "
            "ON s.project_id=c.project_id AND s.id=c.source_id WHERE c.project_id=o.project_id "
            "AND c.organization_id=o.id AND c.review_state='candidate' "
            "AND c.superseded_by IS NULL AND s.superseded_by IS NULL "
            "AND c.source_revision=s.revision) AS has_candidate,"
            "EXISTS(SELECT 1 FROM onefoundry_claims c JOIN onefoundry_sources s "
            "ON s.project_id=c.project_id AND s.id=c.source_id WHERE c.project_id=o.project_id "
            "AND c.organization_id=o.id AND c.review_state='approved' "
            "AND c.superseded_by IS NULL AND s.superseded_by IS NULL "
            "AND c.source_revision=s.revision) AS has_approved_fresh_claim,"
            "EXISTS(SELECT 1 FROM onefoundry_claims c JOIN onefoundry_sources s "
            "ON s.project_id=c.project_id AND s.id=c.source_id WHERE c.project_id=o.project_id "
            "AND c.organization_id=o.id AND (c.superseded_by IS NOT NULL OR "
            "s.superseded_by IS NOT NULL OR c.source_revision<>s.revision)) "
            "AND NOT EXISTS(SELECT 1 FROM onefoundry_claims c JOIN onefoundry_sources s "
            "ON s.project_id=c.project_id AND s.id=c.source_id WHERE c.project_id=o.project_id "
            "AND c.organization_id=o.id AND c.superseded_by IS NULL "
            "AND s.superseded_by IS NULL AND c.source_revision=s.revision) AS has_stale_evidence,"
            "l.work_order_id,l.step_key,ord.status AS order_status,"
            "intent.state AS intent_state,intent.due_at,intent.error_code,"
            "EXISTS(SELECT 1 FROM approval_requests a WHERE a.project_id=o.project_id "
            "AND a.work_order_id=l.work_order_id AND a.status='pending') AS pending_approval "
            "FROM onefoundry_organizations o JOIN onefoundry_workflows w "
            "ON w.project_id=o.project_id AND w.organization_id=o.id "
            "LEFT JOIN LATERAL (SELECT work_order_id,step_key FROM onefoundry_work_order_subjects "
            "WHERE project_id=o.project_id AND organization_id=o.id "
            "ORDER BY created_at DESC,id DESC LIMIT 1) l ON true "
            "LEFT JOIN work_orders ord ON ord.project_id=o.project_id AND ord.id=l.work_order_id "
            "LEFT JOIN LATERAL (SELECT state,due_at,error_code FROM coordination_intents "
            "WHERE project_id=o.project_id AND organization_id=o.id "
            "AND action_kind='onefoundry_draft_outreach' "
            "ORDER BY created_at DESC,id DESC LIMIT 1) intent ON true "
            "WHERE o.project_id=:p ORDER BY o.created_at DESC,o.id DESC"
        ),
        {"p": project_id},
    ).mappings()
    grouped: dict[str, list[dict[str, Any]]] = {"actionable": [], "waiting": [], "blocked": [], "unknown": []}
    for row in rows:
        snapshot = OneFoundryActionSnapshot(
            mode="inbox",
            workflow_status=row["workflow_status"],
            workflow_revision=row["workflow_revision"],
            qualification=row["qualification"],
            source_revision=row["source_revision"],
            has_source=row["has_source"],
            has_candidate=row["has_candidate"],
            has_approved_fresh_claim=row["has_approved_fresh_claim"],
            has_stale_evidence=row["has_stale_evidence"],
            linked_order_status=row["order_status"],
            intent_state=row["intent_state"],
        )
        action = next_action(snapshot).model_dump()
        if row["pending_approval"]:
            action.update(
                action_key="approval",
                label="Review approval",
                reason="A linked work order awaits an operator decision.",
            )
        key = action["action_key"]
        group = (
            "unknown"
            if row["workflow_status"] == "unknown"
            or row["intent_state"] == "unknown"
            or row["order_status"] == "unknown"
            else "blocked"
            if key in {"review_block", "reconcile"}
            else "waiting"
            if key in {"none", "pending_draft"}
            else "actionable"
        )
        grouped[group].append(
            {
                "subject_ref": {"kind": "organization", "id": row["id"]},
                "subject_name": row["display_name"],
                "action": action,
                "reason": action["reason"],
                "required_actor": "operator" if group == "actionable" else None,
                "due_at": row["due_at"] if key == "pending_draft" else None,
                "blocked_reason": row["blocked_reason"] or row["error_code"],
                "evidence_state": "candidate"
                if row["has_candidate"]
                else "approved"
                if row["has_approved_fresh_claim"]
                else "stale"
                if row["has_stale_evidence"]
                else "none",
                "work_order_id": row["work_order_id"],
                "work_order_status": row["order_status"],
                "approval_pending": row["pending_approval"],
            }
        )
    return grouped
