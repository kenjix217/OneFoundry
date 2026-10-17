"""Project skill versions materialized from exact extension decisions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.gateway_extension_skill_prerequisites import resolve_requirements
from onefoundry.skill_schemas import SkillCreate, SkillVersionCreate

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def _materialize(
    gateway: ToolGateway,
    connection: Connection,
    project_id: UUID,
    package_id: UUID | None,
    name: str,
    description: str,
    content: str,
    required_tools: list[str],
) -> tuple[UUID, int]:
    store = gateway._skill_store()
    skill = store.create_skill_in_transaction(
        connection, project_id, SkillCreate(name=name, description=description)
    )
    version = store.add_version_in_transaction(
        connection, project_id, skill["id"], SkillVersionCreate(content=content)
    )
    _record_prerequisites(
        gateway, connection, project_id, package_id, skill["id"], version["version"], required_tools
    )
    return skill["id"], version["version"]


def _record_prerequisites(
    gateway: ToolGateway,
    connection: Connection,
    project_id: UUID,
    package_id: UUID | None,
    skill_id: UUID,
    skill_version: int,
    required_tools: list[str],
) -> None:
    for requirement in resolve_requirements(
        connection, project_id, package_id, required_tools, active_required=True
    ):
        connection.execute(
            text(
                "INSERT INTO extension_skill_prerequisites "
                "(project_id,skill_id,skill_version,package_id,tool_id,"
                "package_sha256,descriptor_sha256,actor_id) "
                "VALUES (:p,:skill,:version,:package,:tool,:package_sha,:descriptor_sha,:actor)"
            ),
            {
                "p": project_id,
                "skill": skill_id,
                "version": skill_version,
                "package": requirement["package_id"],
                "tool": requirement["tool_id"],
                "package_sha": requirement["package_sha256"],
                "descriptor_sha": requirement["descriptor_sha256"],
                "actor": gateway.actor_id,
            },
        )


def _append_existing_draft(
    gateway: ToolGateway, connection: Connection, project_id: UUID, draft: dict[str, Any]
) -> tuple[UUID, int]:
    skill_id: UUID = draft["target_skill_id"]
    # This is the same per-skill transaction lock used by SkillStore's version allocator.
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(CAST(:skill_id AS text)))"),
        {"skill_id": skill_id},
    )
    current = (
        connection.execute(
            text(
                "SELECT s.name,s.description,max(v.version) AS latest_version FROM skills s "
                "JOIN skill_versions v ON v.project_id=s.project_id AND v.skill_id=s.id "
                "WHERE s.project_id=:p AND s.id=:skill GROUP BY s.name,s.description"
            ),
            {"p": project_id, "skill": skill_id},
        )
        .mappings()
        .one_or_none()
    )
    if current is None:
        raise NotFound("existing skill version not found")
    if (current["name"], current["description"]) != (draft["name"], draft["description"]):
        raise Conflict("existing skill metadata changed")
    if current["latest_version"] != draft["expected_base_version"]:
        raise Conflict("existing skill version changed; refresh before accepting")
    version = gateway._skill_store().add_version_in_transaction(
        connection, project_id, skill_id, SkillVersionCreate(content=draft["content"])
    )
    _record_prerequisites(
        gateway,
        connection,
        project_id,
        draft["package_id"],
        skill_id,
        version["version"],
        draft["required_tools"],
    )
    return skill_id, version["version"]


def accept_skill_draft(
    gateway: ToolGateway, connection: Connection, project_id: UUID, draft: dict[str, Any]
) -> tuple[UUID, int]:
    if draft["target_skill_id"] is not None:
        return _append_existing_draft(gateway, connection, project_id, draft)
    return _materialize(
        gateway,
        connection,
        project_id,
        draft["package_id"],
        draft["name"],
        draft["description"],
        draft["content"],
        draft["required_tools"],
    )


def accept_package_skills(
    gateway: ToolGateway, connection: Connection, project_id: UUID, package: dict[str, Any]
) -> None:
    for item in package["manifest"].get("skills", []):
        existing = connection.scalar(
            text(
                "SELECT 1 FROM extension_package_skills WHERE project_id=:p AND package_id=:package "
                "AND skill_key=:key"
            ),
            {"p": project_id, "package": package["id"], "key": item["id"]},
        )
        if existing:
            continue
        name = f"{package['package_key']}: {item['name']}"
        if len(name) > 120:
            name = f"{package['package_key'][:30]}: {item['name'][:70]} [{item['id'][:12]}]"
        prior = connection.scalar(
            text(
                "SELECT ps.skill_id FROM extension_package_skills ps "
                "JOIN extension_packages p ON p.project_id=ps.project_id AND p.id=ps.package_id "
                "WHERE ps.project_id=:project AND p.package_key=:package_key "
                "AND ps.skill_key=:skill_key ORDER BY p.created_at DESC LIMIT 1"
            ),
            {"project": project_id, "package_key": package["package_key"], "skill_key": item["id"]},
        )
        if prior is None:
            skill_id, version = _materialize(
                gateway,
                connection,
                project_id,
                package["id"],
                name,
                item["description"],
                item["content"],
                item["required_tools"],
            )
        else:
            skill_id = prior
            skill_version = gateway._skill_store().add_version_in_transaction(
                connection, project_id, skill_id, SkillVersionCreate(content=item["content"])
            )
            version = skill_version["version"]
            try:
                with connection.begin_nested():
                    connection.execute(
                        text(
                            "UPDATE skills SET name=:name,description=:description "
                            "WHERE project_id=:project AND id=:skill"
                        ),
                        {
                            "name": name,
                            "description": item["description"],
                            "project": project_id,
                            "skill": skill_id,
                        },
                    )
            except IntegrityError as exc:
                if getattr(exc.orig, "sqlstate", None) == "23505":
                    raise Conflict("package skill name already exists") from exc
                raise
            _record_prerequisites(
                gateway,
                connection,
                project_id,
                package["id"],
                skill_id,
                version,
                item["required_tools"],
            )
        connection.execute(
            text(
                "INSERT INTO extension_package_skills "
                "(project_id,package_id,skill_key,skill_id,skill_version,actor_id) "
                "VALUES (:p,:package,:key,:skill,:version,:actor)"
            ),
            {
                "p": project_id,
                "package": package["id"],
                "key": item["id"],
                "skill": skill_id,
                "version": version,
                "actor": gateway.actor_id,
            },
        )
