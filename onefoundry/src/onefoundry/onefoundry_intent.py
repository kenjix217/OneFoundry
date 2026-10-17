"""Built-in qualified-organization continuation in the general coordination queue."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from onefoundry.coordination_store import record_signal


def enqueue_draft_intent(
    connection: Any,
    gateway: Any,
    project_id: UUID,
    organization_id: UUID,
) -> UUID | None:
    workflow = (
        connection.execute(
            text(
                "SELECT id,status,revision FROM onefoundry_workflows WHERE project_id=:p "
                "AND organization_id=:o FOR UPDATE"
            ),
            {"p": project_id, "o": organization_id},
        )
        .mappings()
        .one()
    )
    if workflow["status"] != "research_reviewed":
        return None
    signal_id = record_signal(
        gateway,
        connection,
        project_id,
        source="internal",
        signal_type="organization.qualified",
        dedupe_key=f"organization:{organization_id}:qualified:{workflow['revision']}",
        fields={"organization_id": str(organization_id), "workflow_id": str(workflow["id"])},
        state="matched",
    )
    if signal_id is None:
        return None
    intent_id = uuid4()
    inserted = connection.execute(
        text(
            "INSERT INTO coordination_intents (id,project_id,actor_id,workflow_id,organization_id,"
            "transition_revision,step_key,action_kind,signal_id) "
            "VALUES (:id,:p,:actor,:w,:o,:revision,'draft_outreach','onefoundry_draft_outreach',:signal) "
            "ON CONFLICT (project_id,workflow_id,transition_revision,step_key) DO NOTHING"
        ),
        {
            "id": intent_id,
            "p": project_id,
            "actor": gateway.actor_id,
            "w": workflow["id"],
            "o": organization_id,
            "revision": workflow["revision"],
            "signal": signal_id,
        },
    )
    if inserted.rowcount != 1:
        return None
    gateway._event(
        connection,
        project_id,
        None,
        None,
        "onefoundry_intent_queued",
        {"intent_id": str(intent_id), "workflow_id": str(workflow["id"]), "step": "draft_outreach"},
    )
    return intent_id


def latest_intent(connection: Any, project_id: UUID, organization_id: UUID) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                "SELECT id,state,attempts,due_at,lease_until,error_code,work_order_id,created_at "
                "FROM coordination_intents WHERE project_id=:p AND organization_id=:o "
                "AND action_kind='onefoundry_draft_outreach' "
                "ORDER BY created_at DESC,id DESC LIMIT 1"
            ),
            {"p": project_id, "o": organization_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None
