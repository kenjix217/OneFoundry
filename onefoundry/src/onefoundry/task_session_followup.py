"""A coalesced message never converts an uncertain turn into an automatic replay."""

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text

from onefoundry.task_session_uncertainty import unresolved


def purpose(
    connection: Any, project_id: UUID, attempt: Any, message_id: UUID
) -> Literal["work", "discussion"] | None:
    unknown = unresolved(connection, project_id, root_id=attempt["root_work_order_id"])
    if unknown and attempt["kind"] != "main":
        return None
    requested_discussion = connection.scalar(
        text(
            "SELECT 1 FROM events WHERE project_id=:p AND event_type='chief_workspace_message' "
            "AND payload->>'message_id'=:m AND payload->>'purpose'='discussion'"
        ),
        {"p": project_id, "m": str(message_id)},
    )
    return "discussion" if unknown or attempt["purpose"] == "discussion" or requested_discussion else "work"
