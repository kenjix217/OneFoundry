"""Exact approval-bound maintenance lease for read-only repository setup."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.capability_setup_host_contract import HostSetupJob, HostSetupReceipt
from onefoundry.gateway_errors import Conflict
from onefoundry.repository_code_contract import digest
from onefoundry.runner_spool import RunnerSpool
from onefoundry.runtime_maintenance import LEASE_NAME, active_count, for_gateway
from runner.repository_transport import publish_new

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def approved_job(connection: Any, project_id: UUID, approval_id: UUID, proposal_digest: str) -> HostSetupJob:
    row = (
        connection.execute(
            text(
                "SELECT e.payload->'binding' AS binding,a.target,a.content "
                "FROM events e JOIN approval_requests a "
                "ON a.project_id=e.project_id AND a.id::text=e.payload->>'approval_id' "
                "WHERE e.project_id=:p AND a.id=:id AND a.status='approved' AND a.proposal_digest=:digest "
                "AND e.event_type='capability_setup_proposed' LIMIT 1"
            ),
            {"p": project_id, "id": approval_id, "digest": proposal_digest},
        )
        .mappings()
        .one_or_none()
    )
    if row is None or not row["binding"]["request"].get("repository_alias"):
        raise Conflict("runtime lease requires this exact approved repository setup")
    job = HostSetupJob(
        job_id=approval_id, project_id=project_id, proposal_digest=proposal_digest, **dict(row)
    )
    if digest({"binding": job.binding, "target": job.target, "content": job.content}) != proposal_digest:
        raise Conflict("repository setup identity changed")
    return job


def lease_for(job: HostSetupJob) -> dict[str, str]:
    return {
        "kind": job.kind,
        "job_id": str(job.job_id),
        "project_id": str(job.project_id),
        "proposal_digest": job.proposal_digest,
    }


def acquire(gateway: ToolGateway, project_id: UUID, approval_id: UUID, proposal_digest: str) -> bool:
    gate = for_gateway(gateway)
    try:
        with gate.guard(exclusive=True, allow_maintenance=True) as connection:
            job = approved_job(connection, project_id, approval_id, proposal_digest)
            lease = lease_for(job)
            path = gate.spool / LEASE_NAME
            if path.exists():
                return bool(json.loads(RunnerSpool._read_regular(path, 1024)) == lease)
            if active_count(connection, RunnerSpool(str(gate.spool))):
                return False
            publish_new(path, json.dumps(lease, sort_keys=True).encode())
            gateway._event(
                connection,
                project_id,
                UUID(job.binding["work_order_id"]),
                None,
                "capability_setup_maintenance_started",
                lease,
            )
            return True
    except SQLAlchemyError:
        return False


def release(gateway: ToolGateway, project_id: UUID, approval_id: UUID) -> bool:
    gate = for_gateway(gateway)
    with gate.guard(exclusive=True, allow_maintenance=True) as connection:
        path = gate.spool / LEASE_NAME
        if not path.exists():
            return True
        lease = json.loads(RunnerSpool._read_regular(path, 1024))
        job = approved_job(connection, project_id, approval_id, lease["proposal_digest"])
        if lease != lease_for(job):
            raise Conflict("runtime lease belongs to another setup")
        observed = RunnerSpool(str(gate.spool)).read_receipt(approval_id)
        if observed is None:
            raise Conflict("setup has no observed terminal receipt")
        receipt = HostSetupReceipt.model_validate(observed[0])
        if (
            receipt.job_id != approval_id
            or receipt.project_id != project_id
            or receipt.proposal_digest != job.proposal_digest
            or receipt.status == "unknown"
        ):
            raise Conflict("unknown or changed setup needs reconciliation before new writes")
        gateway._event(
            connection,
            project_id,
            UUID(job.binding["work_order_id"]),
            None,
            "capability_setup_maintenance_finished",
            {**lease, "receipt_sha256": observed[1]},
        )
        path.unlink()
        return True
