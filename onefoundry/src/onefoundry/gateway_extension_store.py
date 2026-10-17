"""Project-owned inert package, connection, and skill-review persistence."""

# Interpolated SQL fragments below are module constants.
# ruff: noqa: S608

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from onefoundry_sdk.package import PackageError, PackageInspection, build_package, inspect_package
from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.gateway_extension_review import request_review
from onefoundry.gateway_extension_schemas import (
    ConnectionBindingPut,
    ConnectionConfig,
    ConnectionCreate,
    ConnectionPatch,
    DraftCreate,
    DraftDecision,
    PackageChange,
    PackageCreate,
)
from onefoundry.gateway_extension_transport import TransportFailure, validate_endpoint

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

PACKAGE_COLUMNS = (
    "id,project_id,actor_id,package_key,package_version,sha256,state,revision,manifest,created_at"
)
CONNECTION_COLUMNS = "id,project_id,actor_id,package_id,template_id,label,config,enabled,version,created_at"
BINDING_COLUMNS = "package_id,slot,connection_id,version"
DRAFT_COLUMNS = (
    "id,project_id,actor_id,kind,name,description,content,required_tools,package_id,"
    "target_skill_id,expected_base_version,source_run_id,sha256,status,"
    "accepted_skill_id,accepted_skill_version,created_at"
)


def _config(
    gateway: ToolGateway, value: ConnectionConfig, template_kind: str, archive: bytes
) -> dict[str, Any]:
    if value.transport == "stdio":
        if template_kind != "mcp":
            raise Conflict("stdio transport requires an MCP connection template")
        if value.stdio_module not in inspect_package(archive).file_sha256:
            raise Conflict("stdio module is not present in the extension package")
        return value.model_dump(mode="json")
    settings = gateway.live_text_settings
    allowed: tuple[str, ...] = getattr(settings, "extension_allowed_origins", ()) if settings else ()
    try:
        validate_endpoint(value.endpoint, allowed)
    except TransportFailure as error:
        raise Conflict(f"extension endpoint rejected: {error}") from None
    return value.model_dump(mode="json")


def _lock_project(gateway: ToolGateway, connection: Connection, project_id: UUID) -> None:
    # Serialize state checks without upgrading foreign-key KEY SHARE locks
    # already held by approval/event writes in the same transaction.
    status = connection.scalar(
        text("SELECT status FROM projects WHERE id=:p FOR NO KEY UPDATE"), {"p": project_id}
    )
    if status is None:
        raise NotFound("project not found")
    if status != "active":
        raise Conflict("project is not active")


def _compatible_templates(target: dict[str, Any], source: dict[str, Any]) -> bool:
    if target["kind"] != source["kind"]:
        return False
    source_operations = {item["id"]: item for item in source.get("operations", [])}
    return all(
        source_operations.get(operation["id"]) == operation for operation in target.get("operations", [])
    )


class ExtensionStore:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    def list_packages(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        f"SELECT {PACKAGE_COLUMNS} FROM extension_packages WHERE project_id=:p "
                        "ORDER BY package_key,package_version,id"
                    ),
                    {"p": project_id},
                ).mappings()
            ]

    def get_package(self, project_id: UUID, package_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(f"SELECT {PACKAGE_COLUMNS} FROM extension_packages WHERE project_id=:p AND id=:id"),
                    {"p": project_id, "id": package_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("extension package not found")
            return dict(row)

    def create_package(self, project_id: UUID, data: PackageCreate) -> dict[str, Any]:
        try:
            archive = build_package(
                data.manifest, {name: content.encode("utf-8") for name, content in data.files.items()}
            )
        except PackageError as error:
            raise Conflict(f"extension package is invalid: {error}") from None
        return self.create_package_archive(project_id, archive)

    def create_package_archive(self, project_id: UUID, archive: bytes) -> dict[str, Any]:
        """Save validated native package bytes without changing their identity."""
        try:
            inspected = inspect_package(archive)
        except PackageError as error:
            raise Conflict(f"extension package is invalid: {error}") from None
        return self._insert_package(project_id, archive, inspected)

    def _insert_package(
        self, project_id: UUID, archive: bytes, inspected: PackageInspection
    ) -> dict[str, Any]:
        package_id = uuid4()
        with self.gateway.engine.begin() as connection:
            _lock_project(self.gateway, connection, project_id)
            try:
                row = (
                    connection.execute(
                        text(
                            "INSERT INTO extension_packages "
                            "(id,project_id,actor_id,package_key,package_version,sha256,manifest,archive) "
                            "VALUES (:id,:p,:actor,:key,:version,:sha,:manifest,:archive) "
                            f"RETURNING {PACKAGE_COLUMNS}"
                        ).bindparams(bindparam("manifest", type_=JSONB)),
                        {
                            "id": package_id,
                            "p": project_id,
                            "actor": self.gateway.actor_id,
                            "key": inspected.manifest.id,
                            "version": inspected.manifest.version,
                            "sha": inspected.package_sha256,
                            "manifest": inspected.manifest.model_dump(mode="json"),
                            "archive": archive,
                        },
                    )
                    .mappings()
                    .one()
                )
            except IntegrityError as error:
                if getattr(error.orig, "sqlstate", None) == "23505":
                    raise Conflict("extension package version already exists") from None
                raise
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "extension_package_saved",
                {
                    "package_id": str(package_id),
                    "sha256": inspected.package_sha256,
                    "version": inspected.manifest.version,
                    "member_count": len(inspected.file_sha256),
                },
            )
            return dict(row)

    def request_package_change(
        self, project_id: UUID, package_id: UUID, action: str, data: PackageChange
    ) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            _lock_project(self.gateway, connection, project_id)
            row = (
                connection.execute(
                    text(
                        f"SELECT {PACKAGE_COLUMNS} FROM extension_packages "
                        "WHERE project_id=:p AND id=:id FOR UPDATE"
                    ),
                    {"p": project_id, "id": package_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("extension package not found")
            package = dict(row)
            prior = (
                connection.execute(
                    text(
                        "SELECT b.approval_id,a.status,a.work_order_id FROM extension_review_bindings b "
                        "JOIN approval_requests a ON a.project_id=b.project_id AND a.id=b.approval_id "
                        "WHERE b.project_id=:p AND b.package_id=:id AND b.action=:action "
                        "AND b.expected_sha256=:sha AND b.expected_revision=:revision"
                    ),
                    {
                        "p": project_id,
                        "id": package_id,
                        "action": action,
                        "sha": data.expected_sha256,
                        "revision": data.expected_revision,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if prior is not None and package["package_version"] == data.expected_version:
                return {
                    "approval_id": prior["approval_id"],
                    "status": prior["status"],
                    "review_work_order_id": prior["work_order_id"],
                }
            if (
                package["sha256"] != data.expected_sha256
                or package["package_version"] != data.expected_version
                or package["revision"] != data.expected_revision
            ):
                raise Conflict("extension package changed; refresh before review")
            if action == "activate" and package["state"] not in {"draft", "revoked"}:
                raise Conflict("extension package is already active")
            if action == "revoke" and package["state"] != "active":
                raise Conflict("extension package is not active")
            return request_review(self.gateway, connection, project_id, package, action, data.source_run_id)

    def list_connections(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        f"SELECT {CONNECTION_COLUMNS} FROM extension_connections "
                        "WHERE project_id=:p ORDER BY label,id"
                    ),
                    {"p": project_id},
                ).mappings()
            ]

    def create_connection(self, project_id: UUID, data: ConnectionCreate) -> dict[str, Any]:
        connection_id = uuid4()
        with self.gateway.engine.begin() as connection:
            _lock_project(self.gateway, connection, project_id)
            package = (
                connection.execute(
                    text("SELECT manifest,archive FROM extension_packages WHERE project_id=:p AND id=:id"),
                    {"p": project_id, "id": data.package_id},
                )
                .mappings()
                .one_or_none()
            )
            if package is None:
                raise NotFound("extension package not found")
            templates = {item["id"]: item for item in package["manifest"].get("connections", [])}
            if data.template_id not in templates:
                raise Conflict("extension connection template is not declared by package")
            config = _config(
                self.gateway, data.config, templates[data.template_id]["kind"], package["archive"]
            )
            try:
                row = (
                    connection.execute(
                        text(
                            "INSERT INTO extension_connections "
                            "(id,project_id,actor_id,package_id,template_id,label,config) "
                            "VALUES (:id,:p,:actor,:package,:template,:label,:config) "
                            f"RETURNING {CONNECTION_COLUMNS}"
                        ).bindparams(bindparam("config", type_=JSONB)),
                        {
                            "id": connection_id,
                            "p": project_id,
                            "actor": self.gateway.actor_id,
                            "package": data.package_id,
                            "template": data.template_id,
                            "label": data.label,
                            "config": config,
                        },
                    )
                    .mappings()
                    .one()
                )
            except IntegrityError as error:
                if getattr(error.orig, "sqlstate", None) == "23505":
                    raise Conflict("extension connection label already exists") from None
                raise
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "extension_connection_saved",
                {"connection_id": str(connection_id), "package_id": str(data.package_id)},
            )
            return dict(row)

    def update_connection(
        self, project_id: UUID, connection_id: UUID, data: ConnectionPatch
    ) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            _lock_project(self.gateway, connection, project_id)
            current = (
                connection.execute(
                    text(
                        f"SELECT {CONNECTION_COLUMNS} FROM extension_connections "
                        "WHERE project_id=:p AND id=:id FOR UPDATE"
                    ),
                    {"p": project_id, "id": connection_id},
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise NotFound("extension connection not found")
            if current["version"] != data.expected_version:
                raise Conflict("extension connection changed; refresh before saving")
            package = (
                connection.execute(
                    text(
                        "SELECT manifest,archive,state FROM extension_packages WHERE project_id=:p AND id=:id"
                    ),
                    {"p": project_id, "id": current["package_id"]},
                )
                .mappings()
                .one()
            )
            templates = {item["id"]: item for item in package["manifest"].get("connections", [])}
            template = templates.get(current["template_id"])
            if template is None:
                raise Conflict("extension connection template is unavailable")
            config = (
                _config(self.gateway, data.config, template["kind"], package["archive"])
                if data.config is not None
                else current["config"]
            )
            enabled = data.enabled if data.enabled is not None else current["enabled"]
            if enabled:
                if package["state"] != "active":
                    raise Conflict("extension package must be active before enabling connection")
            row = (
                connection.execute(
                    text(
                        "UPDATE extension_connections SET config=:config,enabled=:enabled,version=version+1 "
                        "WHERE project_id=:p AND id=:id "
                        f"RETURNING {CONNECTION_COLUMNS}"
                    ).bindparams(bindparam("config", type_=JSONB)),
                    {"config": config, "enabled": enabled, "p": project_id, "id": connection_id},
                )
                .mappings()
                .one()
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "extension_connection_updated",
                {"connection_id": str(connection_id), "version": row["version"], "enabled": enabled},
            )
            return dict(row)

    def list_connection_bindings(self, project_id: UUID, package_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            if (
                connection.scalar(
                    text("SELECT 1 FROM extension_packages WHERE project_id=:p AND id=:package"),
                    {"p": project_id, "package": package_id},
                )
                is None
            ):
                raise NotFound("extension package not found")
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        f"SELECT {BINDING_COLUMNS} FROM extension_connection_bindings "
                        "WHERE project_id=:p AND package_id=:package ORDER BY slot"
                    ),
                    {"p": project_id, "package": package_id},
                ).mappings()
            ]

    def put_connection_binding(
        self, project_id: UUID, package_id: UUID, slot: str, data: ConnectionBindingPut
    ) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            _lock_project(self.gateway, connection, project_id)
            target = connection.scalar(
                text("SELECT manifest FROM extension_packages WHERE project_id=:p AND id=:package"),
                {"p": project_id, "package": package_id},
            )
            if target is None:
                raise NotFound("extension package not found")
            target_template = next(
                (item for item in target.get("connections", []) if item["id"] == slot), None
            )
            if target_template is None:
                raise Conflict("extension connection slot is not declared by package")
            source = (
                connection.execute(
                    text(
                        "SELECT c.package_id,c.template_id,p.manifest FROM extension_connections c "
                        "JOIN extension_packages p ON p.project_id=c.project_id AND p.id=c.package_id "
                        "WHERE c.project_id=:p AND c.id=:connection"
                    ),
                    {"p": project_id, "connection": data.connection_id},
                )
                .mappings()
                .one_or_none()
            )
            if source is None:
                raise NotFound("extension connection not found")
            source_template = next(
                (
                    item
                    for item in source["manifest"].get("connections", [])
                    if item["id"] == source["template_id"]
                ),
                None,
            )
            if source_template is None or not _compatible_templates(target_template, source_template):
                raise Conflict("extension connection does not support this slot")
            current = (
                connection.execute(
                    text(
                        "SELECT connection_id,version FROM extension_connection_bindings "
                        "WHERE project_id=:p AND package_id=:package AND slot=:slot FOR UPDATE"
                    ),
                    {"p": project_id, "package": package_id, "slot": slot},
                )
                .mappings()
                .one_or_none()
            )
            version = current["version"] if current is not None else 0
            if version != data.expected_version:
                raise Conflict("extension connection binding changed; refresh before saving")
            if current is None:
                statement = text(
                    "INSERT INTO extension_connection_bindings "
                    "(project_id,package_id,slot,connection_id,actor_id) "
                    "VALUES (:p,:package,:slot,:connection,:actor) "
                    f"RETURNING {BINDING_COLUMNS}"
                )
            else:
                statement = text(
                    "UPDATE extension_connection_bindings "
                    "SET connection_id=:connection,actor_id=:actor,version=version+1 "
                    "WHERE project_id=:p AND package_id=:package AND slot=:slot "
                    f"RETURNING {BINDING_COLUMNS}"
                )
            row = (
                connection.execute(
                    statement,
                    {
                        "p": project_id,
                        "package": package_id,
                        "slot": slot,
                        "connection": data.connection_id,
                        "actor": self.gateway.actor_id,
                    },
                )
                .mappings()
                .one()
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "extension_connection_bound",
                {
                    "package_id": str(package_id),
                    "slot": slot,
                    "connection_id": str(data.connection_id),
                    "version": row["version"],
                },
            )
            return dict(row)

    def list_drafts(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        f"SELECT {DRAFT_COLUMNS} FROM extension_drafts WHERE project_id=:p "
                        "ORDER BY created_at DESC,id DESC"
                    ),
                    {"p": project_id},
                ).mappings()
            ]

    def get_draft(self, project_id: UUID, draft_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(f"SELECT {DRAFT_COLUMNS} FROM extension_drafts WHERE project_id=:p AND id=:id"),
                    {"p": project_id, "id": draft_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("extension draft not found")
            return dict(row)

    def create_draft(self, project_id: UUID, data: DraftCreate) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            return self.create_draft_in_transaction(connection, project_id, data, self.gateway.actor_id)

    def create_draft_in_transaction(
        self, connection: Connection, project_id: UUID, data: DraftCreate, actor_id: str
    ) -> dict[str, Any]:
        payload = data.model_dump(mode="json", exclude={"source_run_id"})
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        draft_id = uuid4()
        _lock_project(self.gateway, connection, project_id)
        source_order_id = None
        if data.source_run_id is not None:
            source = (
                connection.execute(
                    text("SELECT status,work_order_id FROM runs WHERE project_id=:p AND id=:id"),
                    {"p": project_id, "id": data.source_run_id},
                )
                .mappings()
                .one_or_none()
            )
            if source is None or source["status"] != "running":
                raise Conflict("source run is not active")
            source_order_id = source["work_order_id"]
        if data.package_id is not None or data.required_tools:
            from onefoundry.gateway_extension_skill_prerequisites import resolve_requirements

            resolve_requirements(
                connection,
                project_id,
                data.package_id,
                data.required_tools,
                active_required=False,
            )
        if data.target_skill_id is not None:
            existing = (
                connection.execute(
                    text(
                        "SELECT s.name,s.description,max(v.version) AS latest_version FROM skills s "
                        "JOIN skill_versions v ON v.project_id=s.project_id AND v.skill_id=s.id "
                        "WHERE s.project_id=:p AND s.id=:skill GROUP BY s.name,s.description"
                    ),
                    {"p": project_id, "skill": data.target_skill_id},
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise NotFound("existing skill version not found")
            if (existing["name"], existing["description"]) != (data.name, data.description):
                raise Conflict("existing skill metadata changed")
            if existing["latest_version"] != data.expected_base_version:
                raise Conflict("existing skill version changed; refresh before drafting")
        row = (
            connection.execute(
                text(
                    "INSERT INTO extension_drafts "
                    "(id,project_id,actor_id,kind,name,description,content,required_tools,package_id,"
                    "target_skill_id,expected_base_version,source_run_id,sha256) VALUES "
                    "(:id,:p,:actor,:kind,:name,:description,:content,:tools,:package,"
                    ":target_skill,:base_version,:run,:sha) "
                    f"RETURNING {DRAFT_COLUMNS}"
                ).bindparams(bindparam("tools", type_=JSONB)),
                {
                    "id": draft_id,
                    "p": project_id,
                    "actor": actor_id,
                    "kind": data.kind,
                    "name": data.name,
                    "description": data.description,
                    "content": data.content,
                    "tools": data.required_tools,
                    "package": data.package_id,
                    "target_skill": data.target_skill_id,
                    "base_version": data.expected_base_version,
                    "run": data.source_run_id,
                    "sha": digest,
                },
            )
            .mappings()
            .one()
        )
        self.gateway._event(
            connection,
            project_id,
            source_order_id,
            data.source_run_id,
            "extension_skill_draft_saved",
            {
                "draft_id": str(draft_id),
                "sha256": digest,
                "source_run_id": str(data.source_run_id) if data.source_run_id else None,
            },
            actor_id=actor_id,
        )
        return dict(row)

    def decide_draft(self, project_id: UUID, draft_id: UUID, data: DraftDecision) -> dict[str, Any]:
        from onefoundry.gateway_extension_skills import accept_skill_draft

        with self.gateway.engine.begin() as connection:
            _lock_project(self.gateway, connection, project_id)
            row = (
                connection.execute(
                    text(
                        f"SELECT {DRAFT_COLUMNS} FROM extension_drafts "
                        "WHERE project_id=:p AND id=:id FOR UPDATE"
                    ),
                    {"p": project_id, "id": draft_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("extension draft not found")
            draft = dict(row)
            if draft["sha256"] != data.expected_sha256:
                raise Conflict("extension draft changed; refresh before deciding")
            if draft["status"] != "pending":
                if draft["status"] == ("accepted" if data.decision == "accept" else "rejected"):
                    return draft
                raise Conflict("extension draft already decided")
            if data.decision == "accept":
                skill_id, version = accept_skill_draft(self.gateway, connection, project_id, draft)
                status = "accepted"
            else:
                skill_id, version, status = None, None, "rejected"
            decided = (
                connection.execute(
                    text(
                        "UPDATE extension_drafts SET status=:status,accepted_skill_id=:skill,"
                        "accepted_skill_version=:version WHERE project_id=:p AND id=:id "
                        f"RETURNING {DRAFT_COLUMNS}"
                    ),
                    {
                        "status": status,
                        "skill": skill_id,
                        "version": version,
                        "p": project_id,
                        "id": draft_id,
                    },
                )
                .mappings()
                .one()
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                draft["source_run_id"],
                "extension_skill_draft_decided",
                {
                    "draft_id": str(draft_id),
                    "decision": data.decision,
                    "sha256": draft["sha256"],
                    "skill_id": str(skill_id) if skill_id else None,
                    "version": version,
                },
            )
            return dict(decided)

    def catalog(self, project_id: UUID) -> dict[str, Any]:
        from onefoundry.gateway_extension_identity import canonical_tool_id
        from onefoundry.role_tool_admission import registered_tool_ids

        packages = self.list_packages(project_id)
        tools: list[dict[str, Any]] = [
            {
                "id": tool_id,
                "version": 1,
                "description": "Built-in OneFoundry capability",
                "source": "builtin",
                "status": "active",
            }
            for tool_id in sorted(registered_tool_ids())
        ]
        skills: list[dict[str, Any]] = []
        for package in packages:
            for tool in package["manifest"].get("tools", []):
                tools.append(
                    {
                        "id": canonical_tool_id(package["package_key"], tool["id"]),
                        "declared_id": tool["id"],
                        "version": 1,
                        "description": tool["description"],
                        "source": "extension",
                        "status": package["state"],
                        "package_id": str(package["id"]),
                    }
                )
            for skill in package["manifest"].get("skills", []):
                skills.append(
                    {
                        "id": skill["id"],
                        "name": skill["name"],
                        "package_id": str(package["id"]),
                        "status": package["state"],
                        "required_tools": [
                            canonical_tool_id(package["package_key"], item)
                            for item in skill["required_tools"]
                        ],
                    }
                )
        return {"tools": tools, "packages": packages, "skills": skills}
