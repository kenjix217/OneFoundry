"""Bind discussion-only turns to their immutable Gateway start event."""

from typing import Any
from uuid import UUID

from sqlalchemy import text

DISCUSSION_INSTRUCTIONS = (
    "This is discussion only. Answer in plain text. No tools, team launch or project mutation "
    "will be performed by this reply. Do not return an action proposal or claim to start, assign "
    "or save work. If the operator requests execution, explain that Start work queues the team "
    "with the saved work order and current message."
)


def is_discussion_turn(connection: Any, project_id: UUID, turn_id: UUID) -> bool:
    return bool(
        connection.scalar(
            text(
                "SELECT payload->>'discussion_only' FROM events WHERE project_id=:p "
                "AND event_type='task_conversation_turn_started' AND payload->>'turn_id'=:turn "
                "ORDER BY created_at DESC,id DESC LIMIT 1"
            ),
            {"p": project_id, "turn": str(turn_id)},
        )
        == "true"
    )
