"""Recognize operator-created direct conversations without admitting historical agent jobs."""

from typing import Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.profile_hierarchy import chief_profile_id


def is_direct_conversation(connection: Any, project_id: UUID, order_id: UUID, profile_id: UUID) -> bool:
    return bool(
        connection.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM work_orders w JOIN chief_conversation_controls c "
                "ON c.project_id=w.project_id AND c.work_order_id=w.id JOIN events e ON "
                "e.project_id=w.project_id AND e.work_order_id=w.id AND e.actor_id=c.actor_id "
                "WHERE w.project_id=:p AND w.id=:o AND w.profile_id=:profile AND "
                "w.parent_work_order_id IS NULL AND c.completion_policy='operator_review' AND "
                "e.event_type='direct_agent_conversation_created' AND "
                "e.payload->>'profile_id'=:profile_text)"
            ),
            {"p": project_id, "o": order_id, "profile": profile_id, "profile_text": str(profile_id)},
        )
    )


def allows_main_discussion(connection: Any, project_id: UUID, order_id: UUID, profile_id: UUID) -> bool:
    return profile_id == chief_profile_id(connection, project_id) or is_direct_conversation(
        connection, project_id, order_id, profile_id
    )
