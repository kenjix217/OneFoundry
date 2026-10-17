"""Keep replies to submitted mail inside their original automatic-work chain."""

from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text

from onefoundry.mail_imap import Parsed


def reply_lineage(connection: Any, project_id: UUID, message: Parsed) -> dict[str, Any]:
    identifiers: list[UUID] = []
    for reference in (message.in_reply_to, *message.references):
        if not reference.startswith("<") or not reference.endswith("@onefoundry.local>"):
            continue
        try:
            identifier = UUID(reference[1 : -len("@onefoundry.local>")])
        except ValueError:
            continue
        if identifier not in identifiers:
            identifiers.append(identifier)
    if not identifiers:
        return {}
    row = (
        connection.execute(
            text(
                "SELECT e.work_order_id,e.run_id,w.chain_depth FROM outbound_emails e "
                "JOIN work_orders w ON w.project_id=e.project_id AND w.id=e.work_order_id "
                "WHERE e.project_id=:p AND e.id IN :ids AND e.attempted_at IS NOT NULL "
                "ORDER BY w.chain_depth DESC,e.attempted_at DESC LIMIT 1"
            ).bindparams(bindparam("ids", expanding=True)),
            {"p": project_id, "ids": identifiers},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return {}
    return {
        "origin_work_order_id": row["work_order_id"],
        "origin_run_id": row["run_id"],
        "chain_depth": row["chain_depth"],
    }
