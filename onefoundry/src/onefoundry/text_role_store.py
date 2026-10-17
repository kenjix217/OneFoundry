"""Scoped SQL used inside ToolGateway for scripted text run state."""

# SQL interpolation uses module constants only; values use binds.
# ruff: noqa: S608

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

OUTPUT_COLUMNS = (
    "id, project_id, run_id, profile_id, actor_id, role, source_kind, "
    "configured_model, text, text_sha256, tokens_used, cost_usd, created_at"
)
PROFILE_COLUMNS = (
    "id, project_id, name, kind, model, instructions, memory_scope, allowed_tools, autonomy_level, configured"
)


def get_order(
    connection: Connection, project_id: UUID, order_id: UUID, *, lock: bool
) -> dict[str, Any] | None:
    lock_clause = " FOR UPDATE" if lock else ""
    row = (
        connection.execute(
            text(
                "SELECT id, project_id, title, instructions, profile_id, status "
                "FROM work_orders WHERE project_id = :project_id AND id = :id" + lock_clause
            ),
            {"project_id": project_id, "id": order_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def get_profile(
    connection: Connection, project_id: UUID, profile_id: UUID, *, lock: bool = False
) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                f"SELECT {PROFILE_COLUMNS} FROM profiles WHERE project_id = :project_id AND id = :id"
                + (" FOR SHARE" if lock else "")
            ),
            {"project_id": project_id, "id": profile_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def get_gtm_profile(connection: Connection, project_id: UUID, *, lock: bool = False) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                f"SELECT {PROFILE_COLUMNS} FROM profiles "
                "WHERE project_id = :project_id AND kind = 'builtin' AND name = 'GTM'"
                + (" FOR SHARE" if lock else "")
            ),
            {"project_id": project_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def list_outputs(connection: Connection, project_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
    rows = (
        connection.execute(
            text(
                f"SELECT {OUTPUT_COLUMNS} FROM text_outputs "
                "WHERE project_id = :project_id AND run_id = :run_id "
                "ORDER BY CASE role WHEN 'Researcher' THEN 0 ELSE 1 END, id"
            ),
            {"project_id": project_id, "run_id": run_id},
        )
        .mappings()
        .all()
    )
    outputs: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        sources = (
            connection.execute(
                text(
                    "SELECT m.id, m.project_id, m.scope, m.text, m.source_result_id "
                    "FROM text_output_sources s JOIN memory_items m "
                    "ON m.project_id = s.source_project_id AND m.id = s.memory_item_id "
                    "WHERE s.project_id = :project_id AND s.text_output_id = :output_id "
                    "ORDER BY s.id"
                ),
                {"project_id": project_id, "output_id": row["id"]},
            )
            .mappings()
            .all()
        )
        item["source_memories"] = [dict(source) for source in sources]
        item["source_memory_ids"] = [source["id"] for source in sources]
        outputs.append(item)
    return outputs


def insert_output(
    connection: Connection,
    *,
    output_id: UUID,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    actor_id: str,
    role: str,
    configured_model: str | None,
    body: str,
    digest: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO text_outputs "
            "(id, project_id, run_id, profile_id, actor_id, role, configured_model, text, text_sha256) "
            "VALUES (:id, :project_id, :run_id, :profile_id, :actor_id, :role, "
            ":configured_model, :text, :text_sha256)"
        ),
        {
            "id": output_id,
            "project_id": project_id,
            "run_id": run_id,
            "profile_id": profile_id,
            "actor_id": actor_id,
            "role": role,
            "configured_model": configured_model,
            "text": body,
            "text_sha256": digest,
        },
    )


def insert_source(
    connection: Connection,
    *,
    source_id: UUID,
    project_id: UUID,
    actor_id: str,
    output_id: UUID,
    source_project_id: UUID,
    memory_item_id: UUID,
) -> None:
    connection.execute(
        text(
            "INSERT INTO text_output_sources "
            "(id, project_id, actor_id, text_output_id, source_project_id, memory_item_id) "
            "VALUES (:id, :project_id, :actor_id, :text_output_id, :source_project_id, :memory_item_id)"
        ),
        {
            "id": source_id,
            "project_id": project_id,
            "actor_id": actor_id,
            "text_output_id": output_id,
            "source_project_id": source_project_id,
            "memory_item_id": memory_item_id,
        },
    )
