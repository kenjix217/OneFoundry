"""Project-scoped outcome counts from saved business owners."""

from typing import Any
from uuid import UUID

from sqlalchemy import text


def outcomes(connection: Any, project_id: UUID) -> dict[str, Any]:
    counts = (
        connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM onefoundry_organizations o WHERE o.project_id=:p "
                "AND EXISTS (SELECT 1 FROM onefoundry_work_order_subjects l "
                "JOIN runs r ON r.project_id=l.project_id AND r.work_order_id=l.work_order_id "
                "WHERE l.project_id=o.project_id AND l.organization_id=o.id "
                "AND l.step_key='research' AND r.status='done') "
                "AND EXISTS (SELECT 1 FROM onefoundry_claims c WHERE c.project_id=o.project_id "
                "AND c.organization_id=o.id AND c.review_state IN ('approved','rejected') "
                "AND c.superseded_by IS NULL)) AS reviewed_research, "
                "(SELECT count(*) FROM onefoundry_organizations WHERE project_id=:p "
                "AND qualification='qualified') AS qualified_districts, "
                "(SELECT count(*) FROM onefoundry_drafts WHERE project_id=:p) AS saved_drafts, "
                "(SELECT count(DISTINCT organization_id) FROM onefoundry_drafts "
                "WHERE project_id=:p) AS districts_with_drafts"
            ),
            {"p": project_id},
        )
        .mappings()
        .one()
    )
    usage = (
        connection.execute(
            text(
                "SELECT count(*) FILTER (WHERE kind='model_attempt') AS attempts, "
                "count(*) FILTER (WHERE kind='model_attempt' AND "
                "(state='unknown' OR observed_tokens IS NULL)) AS unknown_tokens, "
                "count(*) FILTER (WHERE kind='model_attempt' AND "
                "(state='unknown' OR observed_cost_usd IS NULL)) AS unknown_cost, "
                "sum(observed_tokens) FILTER (WHERE kind='model_attempt') AS tokens, "
                "sum(observed_cost_usd) FILTER (WHERE kind='model_attempt') AS cost "
                "FROM onefoundry_budget_reservations WHERE project_id=:p"
            ),
            {"p": project_id},
        )
        .mappings()
        .one()
    )
    return {
        **dict(counts),
        "sent": 0,
        "sent_basis": "No OneFoundry delivery receipt owner is configured.",
        "observed_tokens": int(usage["tokens"])
        if usage["attempts"] and not usage["unknown_tokens"]
        else None,
        "observed_cost_usd": str(usage["cost"]) if usage["attempts"] and not usage["unknown_cost"] else None,
    }
