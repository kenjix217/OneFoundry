"""Bounded project roster with honest capability metadata for team planning."""

from typing import Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.settings import Settings
from onefoundry.task_session_admission import configured_profile, profile_blockers


def team_roster(
    connection: Any, project_id: UUID, settings: Settings, offset: int = 0
) -> list[dict[str, Any]]:
    ids = (
        connection.execute(
            text("SELECT id FROM profiles WHERE project_id=:p ORDER BY name,id LIMIT :limit OFFSET :offset"),
            {"p": project_id, "limit": settings.role_context_max_items, "offset": offset},
        )
        .scalars()
        .all()
    )
    agents: list[dict[str, Any]] = []
    for profile_id in ids:
        p = configured_profile(connection, project_id, profile_id)
        agents.append(
            {
                "profile_id": str(p["id"]),
                "name": p["name"],
                "model": p["model"],
                "provider": p["provider"],
                "autonomy": p["autonomy_level"],
                "configured_tools": p["allowed_tools"],
                "blockers": profile_blockers(p, settings),
            }
        )
    total = int(
        connection.scalar(text("SELECT count(*) FROM profiles WHERE project_id=:p"), {"p": project_id})
    )
    if total > offset + len(agents):
        agents.append(
            {
                "omitted_agents": total - offset - len(agents),
                "next_offset": offset + len(agents),
                "next_step": (
                    "Read onefoundry.list_agents at the next_offset for remaining profiles; never guess."
                ),
            }
        )
    return agents
