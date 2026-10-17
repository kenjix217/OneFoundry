"""Observe approved setup through the real read owner and wake the original task once."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.capability_setup_contract import CapabilitySetup
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_access import snapshot
from onefoundry.role_repository_file import RepositoryReadError, repository_file
from onefoundry.role_tool_admission import RunContext
from onefoundry.self_extension_continuation import wake

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def verify(gateway: ToolGateway, project_id: UUID, binding: dict[str, Any]) -> dict[str, Any]:
    data = CapabilitySetup.model_validate(binding["request"])
    settings = gateway.live_text_settings
    if settings is None:
        return {"status": "blocked", "reason": "setup_configuration_unavailable"}
    observed: list[dict[str, Any]] = []
    with gateway.engine.connect() as connection:
        for requested in data.grants:
            current = snapshot(connection, project_id, requested.profile_id)
            if not set(requested.tool_ids) <= set(current["allowed_tools"]):
                return {"status": "blocked", "reason": "setup_access_changed"}
            if "repository_file_v1" not in requested.tool_ids:
                observed.append({"profile_id": str(requested.profile_id), "status": "granted"})
                continue
            if not settings.team_repository_read_enabled:
                return {"status": "blocked", "reason": "repository_disabled"}
            if data.source_id is None or data.verification_path is None:
                raise Conflict("repository setup verification is missing")
            scope = frozenset(current["allowed_tools"])
            context = RunContext(
                project_id=project_id,
                run_id=UUID(binding["session_id"]),
                work_order_id=UUID(binding["work_order_id"]),
                profile_id=requested.profile_id,
                snapshot_tools=scope,
                current_tools=scope,
                snapshot_memory_scope="project",
                current_memory_scope="project",
                max_items=settings.role_context_max_items,
                current_role_name=current["name"],
                current_role_kind=current["kind"],
                snapshot_role_name=current["name"],
            )
            try:
                result = repository_file(
                    connection,
                    context,
                    {"source_id": str(data.source_id), "path": data.verification_path},
                    settings,
                )
            except RepositoryReadError as exc:
                return {"status": "blocked", "reason": exc.code}
            if not result["found"] or result["revision"] != data.revision:
                return {"status": "blocked", "reason": "setup_source_changed"}
            with gateway.engine.connect() as fresh:
                if snapshot(fresh, project_id, requested.profile_id) != current:
                    return {"status": "blocked", "reason": "setup_access_changed"}
            observed.append(
                {
                    "profile_id": str(requested.profile_id),
                    "status": "verified",
                    "source_id": result["source_id"],
                    "revision": result["revision"],
                    "path": result["path"],
                    "sha256": result["sha256"],
                }
            )
    return {"status": "verified", "checks": observed}


def step(gateway: ToolGateway) -> bool:
    with gateway.engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT e.project_id,e.payload FROM events e "
                    "WHERE e.event_type='capability_setup_decided' "
                    "AND NOT EXISTS (SELECT 1 FROM events done WHERE done.project_id=e.project_id "
                    "AND done.event_type='capability_setup_resumed' "
                    "AND done.payload->>'approval_id'=e.payload->>'approval_id') "
                    "ORDER BY e.created_at,e.id LIMIT 8"
                )
            )
            .mappings()
            .all()
        )
    for row in rows:
        payload = row["payload"]
        binding = payload["binding"]
        if payload["decision"] != "approved":
            observed = {"status": "rejected", "reason": "operator_rejected_setup"}
        else:
            try:
                if binding["request"].get("repository_alias") is not None:
                    from onefoundry.capability_setup_host import dispatch_or_observe

                    host = dispatch_or_observe(gateway, row["project_id"], payload)
                    if host is None:
                        continue
                    if host.status != "configured" or host.source_id is None:
                        observed = {"status": host.status, "reason": host.reason}
                    else:
                        verified_binding = {
                            **binding,
                            "request": {
                                **binding["request"],
                                "repository_alias": None,
                                "source_id": str(host.source_id),
                            },
                        }
                        observed = verify(gateway, row["project_id"], verified_binding)
                else:
                    observed = verify(gateway, row["project_id"], binding)
            except (Conflict, NotFound):
                observed = {"status": "blocked", "reason": "setup_scope_changed"}
        approval_id = UUID(payload["approval_id"])
        order_id = UUID(binding["work_order_id"])

        def record(
            connection: Any,
            *,
            project_id: UUID = row["project_id"],
            work_order_id: UUID = order_id,
            decided_id: UUID = approval_id,
            observation: dict[str, Any] = observed,
        ) -> None:
            gateway._event(
                connection,
                project_id,
                work_order_id,
                None,
                "capability_setup_resumed",
                {"approval_id": str(decided_id), "observation": observation},
            )

        body = (
            f"Setup decision: {payload['decision']}. Verification: {observed['status']}. "
            "Verification evidence: " + json.dumps(observed, sort_keys=True) + ". "
            "Read extension status for the exact setup and current capabilities. "
            "Continue the original task using verified access. If a dependency or implementation "
            "is still missing, resolve that remaining blocker; do not repeat this grant. "
            "A rejected setup grants nothing. Original checkpoint: " + binding["request"]["checkpoint"]
        )
        if wake(gateway, row["project_id"], UUID(binding["session_id"]), approval_id, body, on_queued=record):
            return True
    return False
