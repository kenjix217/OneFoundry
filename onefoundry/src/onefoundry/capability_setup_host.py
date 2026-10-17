"""Gateway side of approved named repository mounts and source registration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.capability_setup_contract import CapabilitySetup
from onefoundry.capability_setup_host_contract import (
    HostSetupCatalog,
    HostSetupJob,
    HostSetupReceipt,
    RepositoryOffer,
)
from onefoundry.gateway_errors import Conflict, Unavailable
from onefoundry.profile_hierarchy import lock_project
from onefoundry.repository_code_contract import digest
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def offers(gateway: ToolGateway, project_id: UUID) -> list[dict[str, Any]]:
    try:
        execution = gateway._execution_gateway()
        value = HostSetupCatalog.model_validate_json(
            execution.spool._read_regular(execution.spool.root / "setup_capabilities.json", 16384)
        )
        age = (datetime.now(UTC) - value.observed_at).total_seconds()
        if not 0 <= age <= execution.capability_max_age_seconds:
            raise ValueError("host setup catalog expired")
        return [item.model_dump(mode="json") for item in value.repositories if item.project_id == project_id]
    except (OSError, ValueError, SpoolInvalid, Unavailable):
        return []


def select(gateway: ToolGateway, project_id: UUID, data: CapabilitySetup) -> dict[str, Any]:
    matches = [item for item in offers(gateway, project_id) if item["alias"] == data.repository_alias]
    if len(matches) != 1 or matches[0]["revision"] != data.revision:
        raise Conflict("named host repository is unavailable or changed; inspect the current offers")
    return matches[0]


def dispatch_or_observe(
    gateway: ToolGateway, project_id: UUID, payload: dict[str, Any]
) -> HostSetupReceipt | None:
    spool = gateway._execution_gateway().spool
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        approval = (
            connection.execute(
                text("SELECT * FROM approval_requests WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": UUID(payload["approval_id"])},
            )
            .mappings()
            .one()
        )
        if approval["status"] != "approved":
            raise Conflict("host setup no longer approved")
        job = HostSetupJob(
            job_id=approval["id"],
            project_id=project_id,
            proposal_digest=approval["proposal_digest"],
            binding=payload["binding"],
            target=approval["target"],
            content=approval["content"],
        )
        if (
            digest({"binding": job.binding, "target": job.target, "content": job.content})
            != job.proposal_digest
        ):
            raise Conflict("host setup identity changed")
        path = spool.root / "jobs" / f"{job.job_id}.json"
        if path.exists():
            if HostSetupJob.model_validate_json(spool._read_regular(path, 32768)) != job:
                raise Conflict("host setup dispatch changed")
        else:
            spool.write_job(job.job_id, job.model_dump(mode="json"), group_readable=True, max_bytes=32768)
    observed = spool.read_receipt(job.job_id)
    if observed is None:
        return None
    receipt = HostSetupReceipt.model_validate(observed[0])
    if (
        receipt.job_id != job.job_id
        or receipt.project_id != project_id
        or receipt.proposal_digest != job.proposal_digest
    ):
        raise Conflict("host setup receipt changed")
    return receipt


def register(
    gateway: ToolGateway, project_id: UUID, approval_id: UUID, proposal_digest: str
) -> dict[str, Any]:
    """After mounting, re-probe exact bytes before an idempotent source selection."""
    from onefoundry.capability_setup_maintenance import approved_job, lease_for
    from onefoundry.runtime_maintenance import LEASE_NAME, for_gateway
    from onefoundry.source_schemas import LocalSourceCreate
    from onefoundry.source_store import SourceStore, verify_local

    settings = gateway.live_text_settings
    if settings is None:
        raise Unavailable("source settings unavailable")
    gate = for_gateway(gateway)
    with gate.guard(exclusive=True, allow_maintenance=True) as connection:
        job = approved_job(connection, project_id, approval_id, proposal_digest)
        lease_path = gate.spool / LEASE_NAME
        if not lease_path.exists() or json.loads(RunnerSpool._read_regular(lease_path, 1024)) != lease_for(
            job
        ):
            raise Conflict("source registration requires this setup's maintenance lease")
        lock_project(connection, project_id)
        value = connection.scalar(
            text(
                "SELECT e.payload->'binding' FROM events e JOIN approval_requests a "
                "ON a.project_id=e.project_id AND a.id::text=e.payload->>'approval_id' "
                "WHERE e.project_id=:p AND a.id=:id AND a.status='approved' AND a.proposal_digest=:digest "
                "AND e.event_type='capability_setup_proposed' LIMIT 1"
            ),
            {"p": project_id, "id": approval_id, "digest": proposal_digest},
        )
        if value is None:
            raise Conflict("source selection requires the exact approved setup")
        data = CapabilitySetup.model_validate(value["request"])
        offer = RepositoryOffer.model_validate(value["source"])
        if data.repository_alias != offer.alias or data.revision != offer.revision:
            raise Conflict("host source approval changed")
        if verify_local(settings, offer.alias) != (offer.branch, offer.revision):
            raise Conflict("mounted source is not the approved revision")
        existing = connection.scalar(
            text(
                "SELECT id FROM project_sources WHERE project_id=:p AND kind='local' "
                "AND root_id='workspace' AND relative_path=:alias AND revision=:revision AND branch=:branch "
                "ORDER BY created_at,id LIMIT 1"
            ),
            {"p": project_id, "alias": offer.alias, "revision": offer.revision, "branch": offer.branch},
        )
        if existing is None:
            selected = SourceStore(gateway.engine, gateway.actor_id, settings).add_local(
                project_id,
                LocalSourceCreate(root_id="workspace", relative_path=offer.alias),
                connection=connection,
            )
            existing = selected["id"]
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p "
                "AND event_type='capability_setup_source_selected' AND payload->>'approval_id'=:id LIMIT 1"
            ),
            {"p": project_id, "id": str(approval_id)},
        )
        result = {"approval_id": str(approval_id), "source_id": str(existing), "revision": offer.revision}
        if prior is None:
            gateway._event(
                connection,
                project_id,
                UUID(value["work_order_id"]),
                None,
                "capability_setup_source_selected",
                result,
            )
        elif prior != result:
            raise Conflict("host source selection changed after completion")
        return result
