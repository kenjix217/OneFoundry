"""Expire abandoned queue entries while excluding the host's active attempt."""

from __future__ import annotations

import fcntl
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from onefoundry.gateway_extension_calls import settle
from onefoundry.gateway_extension_spool import publish_json, read_json

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def expire_queued(gateway: ToolGateway, root: Path, *, now: datetime | None = None) -> None:
    fd = os.open(root / ".attempts.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with gateway.engine.connect() as connection:
            rows = list(
                connection.execute(
                    text(
                        "SELECT i.project_id,i.id,i.worker_resume_from,i.worker_resume_started_at,"
                        "i.created_at,i.started_at,i.reason FROM extension_invocations i "
                        "WHERE i.status='running' AND EXISTS (SELECT 1 FROM extension_run_tools t "
                        "WHERE t.project_id=i.project_id AND t.run_id=i.run_id "
                        "AND t.profile_id=i.profile_id AND t.tool_id=i.tool_id "
                        "AND t.worker_image IS NOT NULL)"
                    )
                ).mappings()
            )
        for row in rows:
            path = root / "jobs" / f"{row['id']}.json"
            if (root / "receipts" / path.name).exists() and not row["worker_resume_from"]:
                continue
            if row["worker_resume_from"] or not path.exists():
                settings = gateway.live_text_settings
                assert settings is not None
                expires = (
                    row["worker_resume_started_at"] or row["started_at"] or row["created_at"]
                ) + timedelta(seconds=settings.extension_worker_queue_seconds)
            else:
                job = read_json(path, 7 * 1024 * 1024)
                if "expires_at" not in job:
                    continue
                expires = datetime.fromisoformat(job["expires_at"])
            if row["reason"] != "cancellation_requested" and expires > (now or datetime.now(UTC)):
                continue
            cancellation = root / "cancellations" / path.name
            if not cancellation.exists():
                publish_json(cancellation, {"schema_version": 1, "invocation_id": str(row["id"])})
            claimed = (root / "claims" / path.name).exists()
            settle(
                gateway,
                row["project_id"],
                row["id"],
                "unknown" if claimed else "cancelled",
                reason="worker_interrupted"
                if claimed
                else (
                    "operator_cancelled"
                    if row["reason"] == "cancellation_requested"
                    else "worker_queue_expired"
                ),
            )
    finally:
        os.close(fd)
