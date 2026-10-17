"""One project-scoped descriptor resolver used by Tool Gateway consumers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from onefoundry_sdk import ExtensionManifest, ToolSpec
from sqlalchemy import Connection, text

from onefoundry.gateway_extension_identity import canonical_tool_id


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ExtensionTool:
    id: str
    package_id: UUID
    package_sha256: str
    descriptor_sha256: str
    spec: ToolSpec
    manifest: ExtensionManifest
    connection: dict[str, Any] | None

    @property
    def connection_id(self) -> UUID | None:
        return self.connection["id"] if self.connection else None

    @property
    def connection_version(self) -> int | None:
        return self.connection["version"] if self.connection else None

    @property
    def binding_version(self) -> int:
        return int(self.connection.get("binding_version", 0)) if self.connection else 0

    @property
    def requires_worker(self) -> bool:
        return self.spec.runtime == "python" or bool(
            self.connection and self.connection["config"].get("transport") == "stdio"
        )


def definitions(connection: Connection, project_id: UUID) -> list[ExtensionTool]:
    packages = connection.execute(
        text(
            "SELECT id,sha256,manifest FROM extension_packages "
            "WHERE project_id=:p AND state='active' ORDER BY id"
        ),
        {"p": project_id},
    ).mappings()
    result = []
    for package in packages:
        manifest = ExtensionManifest.model_validate(package["manifest"])
        for spec in manifest.tools:
            binding = None
            if spec.connection:
                explicit = (
                    connection.execute(
                        text(
                            "SELECT connection_id,version FROM extension_connection_bindings "
                            "WHERE project_id=:p AND package_id=:package AND slot=:slot"
                        ),
                        {"p": project_id, "package": package["id"], "slot": spec.connection},
                    )
                    .mappings()
                    .one_or_none()
                )
                if explicit is not None:
                    bindings = list(
                        connection.execute(
                            text(
                                "SELECT c.id,c.config,c.version,c.enabled,c.template_id "
                                "FROM extension_connections c JOIN extension_packages p "
                                "ON p.project_id=c.project_id AND p.id=c.package_id "
                                "WHERE c.project_id=:project AND c.id=:id AND c.enabled AND p.state='active'"
                            ),
                            {"project": project_id, "id": explicit["connection_id"]},
                        ).mappings()
                    )
                else:
                    bindings = list(
                        connection.execute(
                            text(
                                "SELECT id,config,version,enabled,template_id FROM extension_connections "
                                "WHERE project_id=:p AND package_id=:package "
                                "AND template_id=:slot AND enabled"
                            ),
                            {"p": project_id, "package": package["id"], "slot": spec.connection},
                        ).mappings()
                    )
                if len(bindings) != 1:
                    continue
                binding = dict(bindings[0])
                binding["binding_version"] = explicit["version"] if explicit is not None else 0
            result.append(
                ExtensionTool(
                    canonical_tool_id(manifest.id, spec.id),
                    package["id"],
                    package["sha256"],
                    digest(spec.model_dump(mode="json")),
                    spec,
                    manifest,
                    binding,
                )
            )
    return result


def registered_ids(connection: Connection, project_id: UUID) -> frozenset[str]:
    return frozenset(tool.id for tool in definitions(connection, project_id))


def snapshot(
    connection: Connection,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    index: int,
    actor_id: str,
    allowed: list[str],
    worker_image: str | None = None,
) -> None:
    if not any(tool_id.startswith("ext_") for tool_id in allowed):
        return
    for tool in definitions(connection, project_id):
        if tool.id not in allowed:
            continue
        connection.execute(
            text(
                "INSERT INTO extension_run_tools (id,project_id,actor_id,run_id,profile_id,transition_index,"
                "tool_id,package_id,package_sha256,descriptor_sha256,connection_id,connection_version,"
                "binding_version,worker_image) "
                "VALUES "
                "(:id,:p,:actor,:run,:profile,:index,:tool,:package,"
                ":package_hash,:descriptor,:connection,:version,:binding_version,:worker_image)"
            ),
            {
                "id": uuid4(),
                "p": project_id,
                "actor": actor_id,
                "run": run_id,
                "profile": profile_id,
                "index": index,
                "tool": tool.id,
                "package": tool.package_id,
                "package_hash": tool.package_sha256,
                "descriptor": tool.descriptor_sha256,
                "connection": tool.connection_id,
                "version": tool.connection_version,
                "binding_version": tool.binding_version,
                "worker_image": worker_image if tool.requires_worker else None,
            },
        )


def pinned(
    connection: Connection,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    tool: ExtensionTool,
    worker_image: str | None = None,
) -> bool:
    pin = (
        connection.execute(
            text(
                "SELECT package_sha256,descriptor_sha256,connection_id,connection_version,"
                "binding_version,worker_image "
                "FROM extension_run_tools "
                "WHERE project_id=:p AND run_id=:run AND profile_id=:profile AND tool_id=:tool "
                "AND transition_index=(SELECT max(transition_index) FROM run_profile_snapshots "
                "WHERE project_id=:p AND run_id=:run)"
            ),
            {"p": project_id, "run": run_id, "profile": profile_id, "tool": tool.id},
        )
        .mappings()
        .one_or_none()
    )
    return pin is not None and (
        pin["package_sha256"],
        pin["descriptor_sha256"],
        pin["connection_id"],
        pin["connection_version"],
        pin["binding_version"],
        pin["worker_image"],
    ) == (
        tool.package_sha256,
        tool.descriptor_sha256,
        tool.connection_id,
        tool.connection_version,
        tool.binding_version,
        worker_image if tool.requires_worker else None,
    )


def advertised(
    connection: Connection,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    allowed: frozenset[str],
    worker_configured: bool,
    worker_image: str | None = None,
) -> list[dict[str, Any]]:
    if not any(tool_id.startswith("ext_") for tool_id in allowed):
        return []
    return [
        {
            "id": tool.id,
            "version": 1,
            "description": tool.spec.description,
            "parameters": tool.spec.parameters,
        }
        for tool in definitions(connection, project_id)
        if tool.id in allowed
        and (
            (
                tool.spec.runtime != "python"
                and not (tool.connection and tool.connection["config"].get("transport") == "stdio")
            )
            or worker_configured
        )
        and pinned(connection, project_id, run_id, profile_id, tool, worker_image)
    ]
