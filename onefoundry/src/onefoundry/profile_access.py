"""Atomic, compare-and-set application of an operator-approved profile tool grant."""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.schemas import ProfilePatch


def snapshot(connection: Connection, project_id: UUID, profile_id: UUID) -> dict[str, Any]:
    row = (
        connection.execute(
            text("SELECT id,name,kind,allowed_tools FROM profiles WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": profile_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("setup profile is not in this project")
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "kind": row["kind"],
        "allowed_tools": sorted(row["allowed_tools"]),
    }


def grant(connection: Connection, project_id: UUID, expected: dict[str, Any], tools: list[str]) -> None:
    current = snapshot(connection, project_id, UUID(expected["id"]))
    if current != expected:
        raise Conflict("agent settings changed; prepare a new setup proposal")
    updated = sorted(set(current["allowed_tools"]) | set(tools))
    ProfilePatch(allowed_tools=updated)
    connection.execute(
        text("UPDATE profiles SET allowed_tools=:tools WHERE project_id=:p AND id=:id").bindparams(
            bindparam("tools", type_=JSONB)
        ),
        {"p": project_id, "id": UUID(expected["id"]), "tools": updated},
    )
