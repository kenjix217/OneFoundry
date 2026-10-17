"""Bounded owner probes that distinguish configuration from engineering work."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from onefoundry.capability_schemas import (
    CapabilityBlocker,
    CapabilityCheck,
    CapabilityReport,
    capability_check,
)
from onefoundry.gateway_errors import Unavailable
from onefoundry.role_repository_file import SHA40, inspect_source
from onefoundry.settings import Settings

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _flag(operation: str, title: str, enabled: bool, setting: str) -> CapabilityCheck:
    blockers = (
        []
        if enabled
        else [
            CapabilityBlocker(
                kind="disabled",
                code=operation + "_disabled",
                owner="settings",
                message=f"{title} is disabled in this installation.",
                next_step=f"Enable {setting} in private configuration; verify runtime readback.",
            )
        ]
    )
    return capability_check(operation, title, "settings", blockers)


def _repository(
    source: dict[str, Any],
    settings: Settings,
    tools: list[str],
    deadline: float,
    project_id: UUID,
    gateway: ToolGateway | None = None,
) -> CapabilityCheck:
    blockers: list[CapabilityBlocker] = []
    if not settings.team_repository_read_enabled:
        blockers.append(
            CapabilityBlocker(
                kind="disabled",
                code="repository_disabled",
                owner="settings",
                message="Team repository reading is disabled.",
                next_step="Enable TEAM_REPOSITORY_READ_ENABLED and verify runtime readback.",
            )
        )
    if "repository_file_v1" not in tools:
        blockers.append(
            CapabilityBlocker(
                kind="permission_required",
                code="repository_permission",
                owner="profiles",
                message="The inspected agent scope has no repository-file read permission.",
                next_step=(
                    "Approve source access, then grant repository_file_v1 to the intended agent in Team."
                ),
            )
        )
    if source["kind"] == "github":
        failure: str | None = "github_source_unobserved"
        if gateway is not None:
            from onefoundry.source_github_file import inspect
            from onefoundry.source_store import SourceStore

            failure = inspect(
                SourceStore(gateway.engine, gateway.actor_id, settings),
                project_id,
                UUID(str(source["id"])),
                deadline,
            )
        if failure is not None:
            blockers.append(
                CapabilityBlocker(
                    kind="unknown" if failure == "github_source_unobserved" else "dependency_unavailable",
                    code=failure,
                    owner="source_store",
                    message="The selected GitHub commit or its account has not passed a current read probe.",
                    next_step="Check the GitHub connection and selected commit; the file reader exists.",
                )
            )
    else:
        failure = inspect_source(source, settings, deadline)
        if failure is not None:
            dependency_failed = failure in {"source_probe_timed_out", "source_dependency_unavailable"}
            blockers.append(
                CapabilityBlocker(
                    kind="dependency_unavailable" if dependency_failed else "configuration_missing",
                    code=failure,
                    owner="role_repository_file" if dependency_failed else "source_store",
                    message=(
                        "Bounded repository verification timed out or its Git dependency is unavailable."
                        if dependency_failed
                        else "Selected source is unavailable or its saved revision changed."
                    ),
                    next_step=(
                        "Check reader dependency and retry bounded inspection; no feature build is needed."
                    )
                    if dependency_failed
                    else (
                        "Mount the exact repository read-only; select its current revision in Project "
                        "sources."
                    ),
                )
            )
    revision = source.get("revision")
    return capability_check(
        "repository_read",
        "Read selected repository",
        "role_repository_file",
        blockers,
        source_id=UUID(str(source["id"])),
        source_name=source.get("display_name"),
        revision=revision if isinstance(revision, str) and SHA40.fullmatch(revision) else None,
    )


def _coding(settings: Settings, gateway: ToolGateway | None) -> CapabilityCheck:
    blockers: list[CapabilityBlocker] = []
    if not settings.external_code_enabled:
        blockers.append(
            CapabilityBlocker(
                kind="disabled",
                code="external_code_disabled",
                owner="settings",
                message="External coding is disabled.",
                next_step="Enable EXTERNAL_CODE_ENABLED and verify runtime readback.",
            )
        )
    if gateway is not None:
        try:
            from onefoundry.repository_code_gateway import capability

            execution = gateway._execution_gateway()
            available: dict[str, str | None] = {}
            for adapter in ("codex_cli", "claude_code"):
                try:
                    capability(execution, adapter)
                    available[adapter] = None
                except Unavailable:
                    available[adapter] = "unavailable"
        except Unavailable:
            available = {}
        if not any(value is None for value in available.values()):
            blockers.append(
                CapabilityBlocker(
                    kind="dependency_unavailable",
                    code="coding_runner_unavailable",
                    owner="execution_gateway",
                    message="No fresh supported coding adapter is connected to this app's execution spool.",
                    next_step=(
                        "Connect a supported runner and verify fresh capabilities; CLI installation is "
                        "insufficient."
                    ),
                )
            )
    else:
        blockers.append(
            CapabilityBlocker(
                kind="unknown",
                code="coding_runner_unobserved",
                owner="execution_gateway",
                message="Coding runner health was not observed in this inspection.",
                next_step="Inspect through the live Tool Gateway before commissioning coding work.",
            )
        )
    return capability_check(
        "project_code_change", "Isolated repository coding runner", "execution_gateway", blockers
    )


def inspect_capabilities(
    project_id: UUID,
    settings: Settings,
    sources: list[dict[str, Any]],
    tools: list[str],
    *,
    profile_id: UUID | None = None,
    gateway: ToolGateway | None = None,
    observed_at: datetime | None = None,
    omitted_sources: int = 0,
) -> CapabilityReport:
    checks = [
        _flag("team_execution", "Team worker policy", settings.team_worker_enabled, "TEAM_WORKER_ENABLED"),
        _flag(
            "public_web_fetch", "Public web access policy", settings.public_web_enabled, "PUBLIC_WEB_ENABLED"
        ),
        _flag(
            "local_recording",
            "Operator-selected recording policy",
            settings.media_capture_enabled,
            "MEDIA_CAPTURE_ENABLED",
        ),
        _flag(
            "video_edit",
            "Local video editing policy",
            settings.video_edit_enabled,
            "VIDEO_EDIT_ENABLED (requires a pinned renderer image and host bridge)",
        ),
        _flag(
            "static_browser",
            "Static browser policy",
            settings.browser_static_enabled,
            "BROWSER_STATIC_ENABLED (requires a pinned browser image and host bridge)",
        ),
        _coding(settings, gateway),
    ]
    search = list(checks[1].blockers)
    if not settings.public_web_search_endpoint:
        search.append(
            CapabilityBlocker(
                kind="configuration_missing",
                code="search_endpoint_missing",
                owner="public_web_client",
                message="Public search has no configured search service.",
                next_step=(
                    "Set PUBLIC_WEB_SEARCH_ENDPOINT; grant public_web_search_v1 to the built-in Researcher."
                ),
            )
        )
    checks.append(
        capability_check(
            "public_web_search", "Public search service configuration", "public_web_client", search
        )
    )
    deadline = monotonic() + settings.local_source_scan_seconds
    checks.extend(
        _repository(
            source,
            settings,
            tools,
            min(deadline, monotonic() + settings.local_source_git_timeout_seconds),
            project_id,
            gateway,
        )
        for source in sources
    )
    if not sources:
        checks.append(
            capability_check(
                "repository_read",
                "Read selected repository",
                "source_store",
                [
                    CapabilityBlocker(
                        kind="configuration_missing",
                        code="source_not_selected",
                        owner="source_store",
                        message="No inspectable repository source is selected in this project.",
                        next_step=(
                            "Select an approved source in Project sources; grant the intended agent "
                            "repository_file_v1."
                        ),
                    )
                ],
            )
        )
    return CapabilityReport(
        project_id=project_id,
        profile_id=profile_id,
        observed_at=observed_at or datetime.now(UTC),
        checks=checks,
        omitted_sources=omitted_sources,
    )
