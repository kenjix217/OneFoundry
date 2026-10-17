"""Drain active work and pause writes for one exact approved local activation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from onefoundry.extension_activation_contract import ExtensionActivationJob, ExtensionActivationReceipt
from onefoundry.gateway_errors import Conflict
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid
from runner.repository_transport import publish_new

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

LOCK_TAG = "onefoundry.runtime_maintenance.v1"
LEASE_NAME = "runtime-maintenance.json"


class RuntimeMaintenance:
    def __init__(self, engine: Engine, spool: Path, lock_seconds: int) -> None:
        # Gate connections never occupy the application pool while a handler
        # opens its own transaction. This prevents nested pool exhaustion.
        self.engine = create_engine(engine.url, poolclass=NullPool)
        self.spool = spool
        self.lock_seconds = lock_seconds

    def active(self) -> bool:
        return (self.spool / LEASE_NAME).exists()

    @contextmanager
    def guard(self, *, exclusive: bool = False, allow_maintenance: bool = False) -> Iterator[Any]:
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('lock_timeout',:value,true)"), {"value": f"{self.lock_seconds}s"}
            )
            function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
            connection.execute(text(f"SELECT {function}(hashtextextended(:tag,0))"), {"tag": LOCK_TAG})
            if self.active() and not allow_maintenance:
                raise Conflict("App update in progress. New work is paused until activation is reconciled.")
            yield connection


def for_gateway(gateway: ToolGateway) -> RuntimeMaintenance:
    settings = gateway.live_text_settings
    if settings is None:
        raise Conflict("runtime maintenance configuration is unavailable")
    return RuntimeMaintenance(
        gateway.engine,
        gateway._execution_gateway().spool.root,
        settings.repository_code_policy.maintenance_lock_seconds,
    )


def active_count(connection: Any, spool: RunnerSpool) -> int:
    count = int(
        connection.scalar(
            text(
                "SELECT (SELECT count(*) FROM task_session_attempts WHERE status IN ('queued','running')) + "
                "(SELECT count(*) FROM external_code_attempts WHERE status='queued') + "
                "(SELECT count(*) FROM runs WHERE status='running') + "
                "(SELECT count(*) FROM task_conversation_turns WHERE status='running') + "
                "(SELECT count(*) FROM remote_inbound WHERE status='processing') + "
                "(SELECT count(*) FROM remote_outbound WHERE status='claimed') + "
                "(SELECT count(*) FROM runner_jobs WHERE status='queued') + "
                "(SELECT count(*) FROM input_files WHERE status='pending') + "
                "(SELECT count(*) FROM video_edit_jobs "
                "WHERE status IN ('queued','running','cancel_requested'))"
            )
        )
    )
    from onefoundry.extension_qa_contract import ExtensionQaReceipt

    rows = (
        connection.execute(
            text("SELECT project_id,payload FROM events WHERE event_type='self_extension_qa_requested'")
        )
        .mappings()
        .all()
    )
    for row in rows:
        job_id = UUID(row["payload"]["tool_call_id"])
        try:
            observed = spool.read_receipt(job_id)
            if observed is None:
                count += 1
                continue
            receipt = ExtensionQaReceipt.model_validate(observed[0])
            if receipt.project_id != row["project_id"] or receipt.job_id != job_id:
                count += 1
        except (OSError, ValueError, SpoolInvalid):
            count += 1
    return count


def preflight(gateway: ToolGateway, project_id: UUID) -> dict[str, object]:
    with gateway.engine.connect() as connection:
        gateway._require_project(connection, project_id)
        active = active_count(connection, gateway._execution_gateway().spool)
    return {"quiescent": active == 0, "active_count": active}


def acquire(gateway: ToolGateway, project_id: UUID, approval_id: UUID, proposal_digest: str) -> bool:
    gate = for_gateway(gateway)
    try:
        with gate.guard(exclusive=True, allow_maintenance=True) as connection:
            value = connection.scalar(
                text(
                    "SELECT e.payload->'manifest' FROM events e JOIN approval_requests a "
                    "ON a.project_id=e.project_id AND a.id::text=e.payload->>'approval_id' "
                    "WHERE e.project_id=:p AND a.id=:id AND a.status='approved' "
                    "AND a.proposal_digest=:digest "
                    "AND e.event_type='self_extension_activation_queued' LIMIT 1"
                ),
                {"p": project_id, "id": approval_id, "digest": proposal_digest},
            )
            if value is None:
                raise Conflict("runtime lease requires this exact approved activation")
            job = ExtensionActivationJob.model_validate(value)
            payload = {
                "job_id": str(job.job_id),
                "project_id": str(project_id),
                "proposal_digest": proposal_digest,
            }
            path = gate.spool / LEASE_NAME
            if path.exists():
                return bool(json.loads(RunnerSpool._read_regular(path, 1024)) == payload)
            if active_count(connection, RunnerSpool(str(gate.spool))) != 0:
                return False
            publish_new(path, json.dumps(payload, sort_keys=True).encode())
            gateway._event(
                connection,
                project_id,
                job.original_work_order_id,
                None,
                "self_extension_maintenance_started",
                payload,
            )
            return True
    except SQLAlchemyError:
        return False  # An active operation has not drained; no lease was acquired.


def release(gateway: ToolGateway, project_id: UUID, approval_id: UUID) -> bool:
    gate = for_gateway(gateway)
    with gate.guard(exclusive=True, allow_maintenance=True) as connection:
        path = gate.spool / LEASE_NAME
        if not path.exists():
            return True
        lease = json.loads(RunnerSpool._read_regular(path, 1024))
        if lease["project_id"] != str(project_id) or lease["job_id"] != str(approval_id):
            raise Conflict("runtime lease belongs to another activation")
        observed = RunnerSpool(str(gate.spool)).read_receipt(approval_id)
        if observed is None:
            raise Conflict("activation has no observed terminal receipt")
        receipt = ExtensionActivationReceipt.model_validate(observed[0])
        if (
            receipt.job_id != approval_id
            or receipt.project_id != project_id
            or receipt.proposal_digest != lease["proposal_digest"]
            or receipt.status == "unknown"
        ):
            raise Conflict("unknown or changed activation must be reconciled before new writes")
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "self_extension_maintenance_finished",
            {**lease, "receipt_sha256": observed[1], "status": receipt.status},
        )
        path.unlink()
        return True
