"""Project-scoped source metadata and limits, without granting execution tools."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.capability_inspection import inspect_capabilities
from onefoundry.capability_schemas import CapabilityBlocker, capability_check
from onefoundry.gateway_errors import NotFound
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.settings import Settings

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def execution_capabilities(
    connection: Any,
    project_id: UUID,
    settings: Settings,
    *,
    profile_id: UUID | None = None,
    gateway: ToolGateway | None = None,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    if connection.scalar(text("SELECT id FROM projects WHERE id=:p"), {"p": project_id}) is None:
        raise NotFound("project not found")
    profile_rows = (
        connection.execute(
            text("SELECT id,allowed_tools FROM profiles WHERE project_id=:p"), {"p": project_id}
        )
        .mappings()
        .all()
    )
    selected = [row for row in profile_rows if profile_id is None or row["id"] == profile_id]
    if profile_id is not None and not selected:
        raise NotFound("agent profile not found in project")
    rows = (
        connection.execute(
            text(
                "SELECT id,display_name,kind,root_id,relative_path,branch,revision,"
                "count(*) OVER() AS source_count FROM project_sources "
                "WHERE project_id=:p ORDER BY created_at,id LIMIT :limit"
            ),
            {"p": project_id, "limit": settings.role_context_max_items},
        )
        .mappings()
        .all()
    )
    safe_rows = [
        dict(row)
        for row in rows
        if not any(contains_sensitive_text(str(value)) for value in row.values() if value is not None)
    ]
    sources = [
        {"id": str(row["id"]), **{key: row[key] for key in ("display_name", "kind", "branch", "revision")}}
        for row in safe_rows
    ]
    can_read = any("repository_file_v1" in tools for tools in (row["allowed_tools"] for row in profile_rows))
    notes = []
    if rows and not can_read:
        notes.append("Repository sources are registered, but no agent has repository-file read permission.")
    if can_read and not settings.team_repository_read_enabled:
        notes.append("Team repository reading requires operator-approved activation in this deployment.")
    if not settings.external_code_enabled:
        notes.append(
            "External coding is disabled. Planning can run; implementation needs the separate coding gateway."
        )
    # Count and bounded records must share a statement snapshot. Concurrent
    # source removal must not produce a negative or contradictory omitted count.
    total_sources = int(rows[0]["source_count"]) if rows else 0
    readiness = inspect_capabilities(
        project_id,
        settings,
        safe_rows,
        sorted({tool for row in selected for tool in row["allowed_tools"]}),
        profile_id=profile_id,
        gateway=gateway,
        observed_at=observed_at,
        omitted_sources=total_sources - len(safe_rows),
    )
    from pydantic import ValidationError

    from onefoundry.repository_code_contract import RepositoryCodeTarget

    targets = (
        connection.execute(
            text(
                "SELECT content FROM approval_requests WHERE project_id=:p "
                "AND action_type='other' AND status='approved' "
                "AND content LIKE :kind ORDER BY created_at DESC LIMIT :limit"
            ),
            {
                "p": project_id,
                "limit": settings.role_context_max_items,
                "kind": "%repository_code_target_v1%",
            },
        )
        .scalars()
        .all()
    )
    approved = False
    for content in targets:
        try:
            target = RepositoryCodeTarget.model_validate_json(content)
        except ValidationError:
            continue
        # The display page is deliberately bounded; it is not the source
        # registry. Resolve the approved identity through the project owner.
        if (
            connection.scalar(
                text(
                    "SELECT id FROM project_sources WHERE project_id=:p AND id=:id "
                    "AND kind='local' AND revision=:revision"
                ),
                {"p": project_id, "id": target.source_id, "revision": target.revision},
            )
            is not None
        ):
            approved = True
            break
    readiness.checks.append(
        capability_check(
            "maintenance_target",
            "Approved maintenance target",
            "repository_code_gateway",
            []
            if approved
            else [
                CapabilityBlocker(
                    kind="configuration_missing",
                    code="maintenance_target_not_selected",
                    owner="repository_code_gateway",
                    message="No exact approved maintenance source is selected for this project.",
                    next_step="Select the maintenance source and approve its exact revision, allowed "
                    "files and isolated test image.",
                )
            ],
        )
    )
    if gateway is not None and profile_id is not None:
        from onefoundry.requested_capability import inspect

        requested = (
            connection.execute(
                text(
                    "SELECT DISTINCT e.payload->>'operation' FROM events e JOIN task_sessions s "
                    "ON s.project_id=e.project_id AND s.id::text=e.payload->>'original_session_id' "
                    "WHERE e.project_id=:p AND s.profile_id=:profile "
                    "AND e.event_type='self_extension_requested' "
                    "AND e.payload->>'operation' LIKE 'tool:%' LIMIT 8"
                ),
                {"p": project_id, "profile": profile_id},
            )
            .scalars()
            .all()
        )
        for operation in requested:
            readiness.checks.append(inspect(gateway, project_id, profile_id, operation[5:]))
    maintenance: dict[str, Any] = {}
    host_repositories: list[dict[str, Any]] = []
    if gateway is not None:
        from onefoundry.capability_setup_host import offers
        from onefoundry.extension_release_capability import inspect as release_capability
        from onefoundry.gateway_errors import Unavailable
        from onefoundry.repository_code_gateway import capability

        try:
            connected = capability(gateway._execution_gateway(), "codex_cli")
            maintenance = connected.model_dump(mode="json")
        except Unavailable:
            pass  # The readiness report above retains the failed owner and next action.
        host_repositories = offers(gateway, project_id)
        maintenance["release_worker"] = release_capability(gateway)
    return {
        "host_repositories": host_repositories,
        "maintenance_runtime": maintenance,
        "sources": sources,
        "external_coding_enabled": settings.external_code_enabled,
        "repository_read_configured": can_read and settings.team_repository_read_enabled,
        "notes": notes,
        "readiness": readiness.model_dump(mode="json"),
        "boundary": "Configured metadata is not a verified live coding or repository connection.",
    }
