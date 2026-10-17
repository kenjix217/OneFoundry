"""Read-only artifact index for the exact team root, using existing run owners."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import NotFound

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def list_team_artifacts(gateway: ToolGateway, project_id: UUID, order_id: UUID) -> list[dict[str, Any]]:
    with gateway.engine.connect() as connection:
        root = connection.scalar(
            text(
                "SELECT root_work_order_id FROM task_sessions WHERE project_id=:p AND work_order_id=:o "
                "AND is_order_owner"
            ),
            {"p": project_id, "o": order_id},
        )
        if root is None:
            raise NotFound("team session not found")
        rows = connection.execute(
            text(
                "SELECT x.id,x.run_id,x.kind,x.mime_type,x.sha256,x.byte_size,x.status,x.created_at,"
                "x.finished_at,s.profile_id,p.name AS profile_name FROM workspace_artifacts x "
                "JOIN task_session_attempts a ON a.project_id=x.project_id AND a.id=x.run_id "
                "JOIN task_sessions s ON s.project_id=a.project_id AND s.id=a.session_id "
                "JOIN profiles p ON p.project_id=s.project_id AND p.id=s.profile_id "
                "WHERE x.project_id=:p AND s.root_work_order_id=:root "
                "ORDER BY x.created_at DESC,x.id LIMIT 100"
            ),
            {"p": project_id, "root": root},
        ).mappings()
        return [dict(row) for row in rows]
