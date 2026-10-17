"""Read the saved coordination cause of work without creating events or model turns."""

from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text


def origins(connection: Any, project_id: UUID, order_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    if not order_ids:
        return {}
    rows = connection.execute(
        text(
            "SELECT o.id,s.id AS signal_id,s.signal_type,s.source,s.created_at AS observed_at,"
            "r.name AS rule_name,i.rule_version FROM work_orders o "
            "JOIN coordination_signals s ON s.project_id=o.project_id AND s.id=o.origin_signal_id "
            "LEFT JOIN LATERAL (SELECT rule_id,rule_version FROM coordination_intents "
            "WHERE project_id=o.project_id AND work_order_id=o.id AND action_kind<>'auto_start' "
            "ORDER BY created_at,id LIMIT 1) i ON true "
            "LEFT JOIN coordination_rules r ON r.project_id=o.project_id AND r.id=i.rule_id "
            "WHERE o.project_id=:p AND o.id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
        {"p": project_id, "ids": order_ids},
    ).mappings()
    return {row["id"]: {key: value for key, value in row.items() if key != "id"} for row in rows}
