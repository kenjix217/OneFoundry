"""Apply only the exact role grant included in a human-approved activation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text

from onefoundry.extension_activation_contract import ExtensionActivationJob
from onefoundry.gateway_errors import Conflict
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.projects import ProjectStore
from onefoundry.role_tool_admission import registered_tool_ids
from onefoundry.runner_spool import RunnerSpool
from onefoundry.runtime_maintenance import LEASE_NAME, for_gateway
from onefoundry.schemas import ProfilePatch

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def configure(gateway: ToolGateway, project_id: UUID, approval_id: UUID) -> bool:
    gate = for_gateway(gateway)
    settings = gateway.live_text_settings
    if settings is None:
        raise Conflict("approved role configuration is unavailable")
    with gate.guard(exclusive=True, allow_maintenance=True) as connection:
        lease = json.loads(RunnerSpool._read_regular(gate.spool / LEASE_NAME, 1024))
        if lease["job_id"] != str(approval_id) or lease["project_id"] != str(project_id):
            raise Conflict("role configuration requires this activation's maintenance lease")
        value = connection.scalar(
            text(
                "SELECT e.payload->'manifest' FROM events e JOIN approval_requests a "
                "ON a.project_id=e.project_id AND a.id::text=e.payload->>'approval_id' "
                "WHERE e.project_id=:p AND a.id=:id AND a.status='approved' "
                "AND a.proposal_digest=:digest AND e.event_type='self_extension_activation_queued' LIMIT 1"
            ),
            {"p": project_id, "id": approval_id, "digest": lease["proposal_digest"]},
        )
        job = ExtensionActivationJob.model_validate(value)
        observed = json.loads(
            RunnerSpool._read_regular(gate.spool / "claims" / f"{approval_id}.installed.json", 1024)
        )
        if observed != {
            "job_id": str(approval_id),
            "proposal_digest": job.proposal_digest,
            "version": job.release.version,
            "image": job.release.app_image,
        }:
            raise Conflict("role grant requires exact installed release readback")
        store = ProjectStore(gateway.engine, gateway.actor_id, ModelSecretStore(settings.model_secret_root))
        for grant in job.tool_grants:
            current = connection.scalar(
                text("SELECT allowed_tools FROM profiles WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": grant.profile_id},
            )
            after = sorted(set(grant.tools_before) | {grant.tool_id})
            if grant.tool_id not in registered_tool_ids() or current not in (grant.tools_before, after):
                raise Conflict("approved tool is unavailable or profile permissions changed")
            if current != after:
                result = store.update_profile(project_id, grant.profile_id, ProfilePatch(allowed_tools=after))
                if result is None:
                    raise Conflict("approved role no longer exists")
            prior = connection.scalar(
                text(
                    "SELECT 1 FROM events WHERE project_id=:p "
                    "AND event_type='self_extension_tool_grant_applied' "
                    "AND payload->>'approval_id'=:id LIMIT 1"
                ),
                {"p": project_id, "id": str(approval_id)},
            )
            if prior is None:
                gateway._event(
                    connection,
                    project_id,
                    job.original_work_order_id,
                    None,
                    "self_extension_tool_grant_applied",
                    {
                        "approval_id": str(approval_id),
                        "profile_id": str(grant.profile_id),
                        "tool_id": grant.tool_id,
                    },
                )
    return True
