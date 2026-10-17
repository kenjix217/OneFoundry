"""Bound remote attempts whose process disappeared after its durable claim."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text

from onefoundry.gateway_extension_calls import settle
from onefoundry.role_tool_admission import ToolOutcomeUnknown

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def recover_remote_attempts(gateway: ToolGateway, *, now: datetime | None = None) -> None:
    settings = gateway.live_text_settings
    if settings is None:
        return
    # The request deadline plus the configured reconciliation window allows an
    # active transport to settle before its abandoned claim becomes unknown.
    cutoff = (now or datetime.now(UTC)) - timedelta(
        seconds=settings.extension_timeout_seconds + settings.extension_worker_queue_seconds
    )
    with gateway.engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT i.project_id,i.id FROM extension_invocations i "
                    "WHERE i.status='running' AND i.started_at < :cutoff AND NOT EXISTS "
                    "(SELECT 1 FROM extension_run_tools t WHERE t.project_id=i.project_id "
                    "AND t.run_id=i.run_id AND t.profile_id=i.profile_id AND t.tool_id=i.tool_id "
                    "AND t.worker_image IS NOT NULL)"
                ),
                {"cutoff": cutoff},
            ).mappings()
        )
    for row in rows:
        try:
            settle(gateway, row["project_id"], row["id"], "unknown", reason="remote_attempt_interrupted")
        except ToolOutcomeUnknown:
            # A live transport or another reconciliation won the terminal CAS.
            continue
