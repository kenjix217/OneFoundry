"""Resolve accepted skill tool requirements to project-owned immutable identities."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from onefoundry_sdk import ExtensionManifest
from sqlalchemy import Connection, text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.gateway_extension_identity import canonical_tool_id
from onefoundry.gateway_extension_registry import digest


def resolve_requirements(
    connection: Connection,
    project_id: UUID,
    package_id: UUID | None,
    required_tools: list[str],
    *,
    active_required: bool,
) -> list[dict[str, Any]]:
    """Accept canonical built-ins/extensions and legacy local IDs in a package draft."""
    if not required_tools:
        return []
    from onefoundry.role_tool_admission import registered_tool_ids

    packages = list(
        connection.execute(
            text("SELECT id,package_key,sha256,manifest,state FROM extension_packages WHERE project_id=:p"),
            {"p": project_id},
        ).mappings()
    )
    selected = next((row for row in packages if row["id"] == package_id), None)
    if package_id is not None and selected is None:
        raise NotFound("extension package not found")
    local_ids = (
        {item["id"] for item in selected["manifest"].get("tools", [])} if selected is not None else set()
    )
    builtins = registered_tool_ids()
    resolved: list[dict[str, Any]] = []
    for requested in required_tools:
        if requested in builtins:
            resolved.append(
                {"tool_id": requested, "package_id": None, "package_sha256": None, "descriptor_sha256": None}
            )
            continue
        candidates = [package for package in packages if package["state"] == "active"]
        if not candidates:
            candidates = packages
        if requested in local_ids and selected is not None:
            candidates = [selected]
        matches: list[dict[str, Any]] = []
        for package in candidates:
            manifest = ExtensionManifest.model_validate(package["manifest"])
            for spec in manifest.tools:
                if requested != canonical_tool_id(package["package_key"], spec.id) and not (
                    package["id"] == package_id and requested == spec.id
                ):
                    continue
                if active_required and package["state"] != "active":
                    continue
                matches.append(
                    {
                        "tool_id": canonical_tool_id(package["package_key"], spec.id),
                        "package_id": package["id"],
                        "package_sha256": package["sha256"],
                        "descriptor_sha256": digest(spec.model_dump(mode="json")),
                    }
                )
        if len(matches) != 1:
            raise Conflict("skill requires one current project tool descriptor")
        resolved.append(matches[0])
    if len({item["tool_id"] for item in resolved}) != len(resolved):
        raise Conflict("skill tool prerequisites must resolve to distinct tools")
    return resolved
