"""Saved attachment identities and exact sections used in a Task turn."""

from typing import Any
from uuid import UUID

from sqlalchemy import text


def turn_input_readback(connection: Any, project_id: UUID, turn_id: UUID) -> dict[str, list[dict[str, Any]]]:
    attachments = (
        connection.execute(
            text(
                "SELECT f.id,f.display_name,f.sha256 FROM input_file_links l "
                "JOIN input_files f ON f.project_id=l.project_id AND f.id=l.file_id "
                "WHERE l.project_id=:project_id AND l.turn_id=:turn_id AND l.scope='turn' "
                "ORDER BY f.display_name,f.id"
            ),
            {"project_id": project_id, "turn_id": turn_id},
        )
        .mappings()
        .all()
    )
    sections = (
        connection.execute(
            text(
                "SELECT u.file_id,f.display_name AS file_name,f.sha256 AS file_sha256,"
                "u.kind,u.source_label,u.sha256 AS chunk_sha256,u.byte_count "
                "FROM input_file_context_uses u JOIN input_files f "
                "ON f.project_id=u.project_id AND f.id=u.file_id "
                "WHERE u.project_id=:project_id AND u.turn_id=:turn_id "
                "ORDER BY f.display_name,u.source_label,u.id"
            ),
            {"project_id": project_id, "turn_id": turn_id},
        )
        .mappings()
        .all()
    )
    return {
        "attachments": [dict(row) for row in attachments],
        "used_sections": [dict(row) for row in sections],
    }
