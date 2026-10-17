"""Project and profile persistence, scoped by project ID."""

import json
from importlib.resources import files
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.profile_hierarchy import chief_profile_id, lock_project, validate_parent
from onefoundry.project_plan import ProjectPlanUpdate, read_plan, update_plan
from onefoundry.schemas import ProfileCreate, ProfilePatch, ProjectBrief, ProjectCreate

BUILTIN_ROLES = ("Chief of Staff", "Engineer", "QA", "Researcher", "GTM", "Sales")


def starter_instructions() -> dict[str, str]:
    raw = json.loads(files("onefoundry").joinpath("starter_roles.json").read_text())
    if (
        not isinstance(raw, dict)
        or set(raw) != set(BUILTIN_ROLES)
        or any(not isinstance(value, str) or not value.strip() for value in raw.values())
    ):
        raise ValueError("invalid starter role configuration")
    return {name: str(raw[name]) for name in BUILTIN_ROLES}


class ProjectStore:
    def __init__(self, engine: Engine, actor_id: str, secrets: ModelSecretStore) -> None:
        self.engine = engine
        self.actor_id = actor_id
        self.secrets = secrets

    def list_projects(self, status: str = "active") -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, name, goal, desired_outcome, status, version, "
                    "trash_from_status, created_at FROM projects "
                    "WHERE status = :status ORDER BY created_at, id"
                ),
                {"status": status},
            ).mappings()
            return [dict(row) for row in rows]

    def get_project(self, project_id: UUID) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id, name, goal, desired_outcome, status, version, "
                        "trash_from_status, created_at "
                        "FROM projects WHERE id = :id"
                    ),
                    {"id": project_id},
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row else None

    def create_project(self, data: ProjectCreate) -> dict[str, Any]:
        project_id = uuid4()
        instructions = starter_instructions()
        with self.engine.begin() as connection:
            project = (
                connection.execute(
                    text(
                        "INSERT INTO projects (id, name, actor_id) VALUES (:id, :name, :actor_id) "
                        "RETURNING id, name, goal, desired_outcome, status, version, "
                        "trash_from_status, created_at"
                    ),
                    {"id": project_id, "name": data.name, "actor_id": self.actor_id},
                )
                .mappings()
                .one()
            )
            connection.execute(
                text("INSERT INTO project_plans (project_id,actor_id) VALUES (:p,:actor)"),
                {"p": project_id, "actor": self.actor_id},
            )
            insert_profile = text(
                "INSERT INTO profiles "
                "(id, project_id, actor_id, name, kind, model, instructions, memory_scope, "
                "allowed_tools, autonomy_level, configured, reports_to_profile_id) "
                "VALUES (:id, :project_id, :actor_id, :name, 'builtin', NULL, :instructions, 'company', "
                ":allowed_tools, 'manual', false, :reports_to_profile_id)"
            ).bindparams(bindparam("allowed_tools", type_=JSONB))
            chief_id = uuid4()
            for name in BUILTIN_ROLES:
                connection.execute(
                    insert_profile,
                    {
                        "id": chief_id if name == "Chief of Staff" else uuid4(),
                        "project_id": project_id,
                        "actor_id": self.actor_id,
                        "name": name,
                        "instructions": instructions[name],
                        "allowed_tools": [],
                        "reports_to_profile_id": None if name == "Chief of Staff" else chief_id,
                    },
                )
            return dict(project)

    def get_plan(self, project_id: UUID) -> dict[str, Any]:
        with self.engine.connect() as connection:
            return read_plan(connection, project_id)

    def update_plan(self, project_id: UUID, data: ProjectPlanUpdate) -> dict[str, Any]:
        with self.engine.begin() as connection:
            return update_plan(connection, project_id, data, self.actor_id)

    def update_brief(self, project_id: UUID, data: ProjectBrief) -> dict[str, Any]:
        with self.engine.begin() as connection:
            current = (
                connection.execute(
                    text("SELECT status, version FROM projects WHERE id=:id FOR UPDATE"),
                    {"id": project_id},
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise NotFound("project not found")
            if current["status"] != "active":
                raise Conflict("restore this project before changing its workspace")
            if current["version"] != data.expected_version:
                raise Conflict("project changed; refresh its saved state")
            updated = (
                connection.execute(
                    text(
                        "UPDATE projects SET goal=:goal, desired_outcome=:desired_outcome, version=version+1 "
                        "WHERE id=:id RETURNING id, name, goal, desired_outcome, status, version, "
                        "trash_from_status, created_at"
                    ),
                    {"id": project_id, "goal": data.goal, "desired_outcome": data.desired_outcome},
                )
                .mappings()
                .one()
            )
            return dict(updated)

    def fill_starter_instructions(self, project_id: UUID) -> list[dict[str, Any]]:
        instructions = starter_instructions()
        with self.engine.begin() as connection:
            status = connection.scalar(
                text("SELECT status FROM projects WHERE id=:id FOR UPDATE"), {"id": project_id}
            )
            if status is None:
                raise NotFound("project not found")
            if status != "active":
                raise Conflict("restore this project before changing its workspace")
            for name, value in instructions.items():
                connection.execute(
                    text(
                        "UPDATE profiles SET instructions=:instructions, configured=(model IS NOT NULL) "
                        "WHERE project_id=:project_id AND kind='builtin' AND name=:name "
                        "AND btrim(instructions)=''"
                    ),
                    {"project_id": project_id, "name": name, "instructions": value},
                )
            rows = connection.execute(
                text(
                    "SELECT id, project_id, name, kind, model, model_account_id, "
                    "reasoning_effort, instructions, "
                    "memory_scope, allowed_tools, autonomy_level, configured, "
                    "reports_to_profile_id FROM profiles "
                    "WHERE project_id=:project_id ORDER BY kind, name, id"
                ),
                {"project_id": project_id},
            ).mappings()
            return [dict(row) for row in rows]

    def list_profiles(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, project_id, name, kind, model, model_account_id, "
                    "reasoning_effort, instructions, memory_scope, "
                    "allowed_tools, autonomy_level, configured, reports_to_profile_id FROM profiles "
                    "WHERE project_id = :project_id ORDER BY kind, name, id"
                ),
                {"project_id": project_id},
            ).mappings()
            return [dict(row) for row in rows]

    def get_profile(self, project_id: UUID, profile_id: UUID) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id, project_id, name, kind, model, model_account_id, reasoning_effort, "
                        "instructions, memory_scope, "
                        "allowed_tools, autonomy_level, configured, reports_to_profile_id FROM profiles "
                        "WHERE project_id = :project_id AND id = :id"
                    ),
                    {"project_id": project_id, "id": profile_id},
                )
                .mappings()
                .one_or_none()
            )
            return dict(row) if row else None

    def create_profile(self, project_id: UUID, data: ProfileCreate) -> dict[str, Any]:
        self._validate_binding(project_id, data.model_account_id, data.model, data.reasoning_effort)
        statement = text(
            "INSERT INTO profiles "
            "(id, project_id, actor_id, name, kind, model, model_account_id, "
            "reasoning_effort, instructions, memory_scope, "
            "allowed_tools, autonomy_level, configured, reports_to_profile_id) "
            "VALUES (:id, :project_id, :actor_id, :name, 'custom', :model, "
            ":model_account_id, :reasoning_effort, :instructions, "
            ":memory_scope, :allowed_tools, :autonomy_level, :configured, :reports_to_profile_id) "
            "RETURNING id, project_id, name, kind, model, model_account_id, "
            "reasoning_effort, instructions, memory_scope, "
            "allowed_tools, autonomy_level, configured, reports_to_profile_id"
        ).bindparams(bindparam("allowed_tools", type_=JSONB))
        with self.engine.begin() as connection:
            lock_project(connection, project_id)
            profile_id = uuid4()
            if "reports_to_profile_id" in data.model_fields_set:
                parent_id = data.reports_to_profile_id
            else:
                parent_id = chief_profile_id(connection, project_id)
            validate_parent(connection, project_id, profile_id, parent_id)
            row = (
                connection.execute(
                    statement,
                    {
                        "id": profile_id,
                        "project_id": project_id,
                        "actor_id": self.actor_id,
                        "name": data.name,
                        "model": data.model,
                        "model_account_id": data.model_account_id,
                        "reasoning_effort": data.reasoning_effort,
                        "configured": bool(data.model and data.instructions),
                        "instructions": data.instructions,
                        "memory_scope": data.memory_scope,
                        "allowed_tools": data.allowed_tools,
                        "autonomy_level": data.autonomy_level,
                        "reports_to_profile_id": parent_id,
                    },
                )
                .mappings()
                .one()
            )
            return dict(row)

    def update_profile(self, project_id: UUID, profile_id: UUID, data: ProfilePatch) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            lock_project(connection, project_id)
            row = (
                connection.execute(
                    text(
                        "SELECT model, model_account_id, reasoning_effort, instructions, memory_scope, "
                        "allowed_tools, autonomy_level, reports_to_profile_id, kind, name "
                        "FROM profiles WHERE project_id = :project_id AND id = :id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": profile_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            values = dict(row)
            values.update(data.model_dump(exclude_unset=True))
            if "reports_to_profile_id" in data.model_fields_set:
                if profile_id == chief_profile_id(connection, project_id):
                    if data.reports_to_profile_id is not None:
                        raise Conflict("Chief of Staff reports directly to the operator")
                else:
                    validate_parent(connection, project_id, profile_id, data.reports_to_profile_id)
            if (values["model_account_id"], values["model"], values["reasoning_effort"]) != (
                row["model_account_id"],
                row["model"],
                row["reasoning_effort"],
            ):
                self._validate_binding(
                    project_id, values["model_account_id"], values["model"], values["reasoning_effort"]
                )
            values["configured"] = bool(values["model"] and values["instructions"])
            values["project_id"] = project_id
            values["id"] = profile_id
            statement = text(
                "UPDATE profiles SET model = :model, model_account_id = :model_account_id, "
                "reasoning_effort = :reasoning_effort, "
                "instructions = :instructions, "
                "memory_scope = :memory_scope, allowed_tools = :allowed_tools, "
                "autonomy_level = :autonomy_level, configured = :configured, "
                "reports_to_profile_id = :reports_to_profile_id "
                "WHERE project_id = :project_id AND id = :id "
                "RETURNING id, project_id, name, kind, model, model_account_id, "
                "reasoning_effort, instructions, memory_scope, "
                "allowed_tools, autonomy_level, configured, reports_to_profile_id"
            ).bindparams(bindparam("allowed_tools", type_=JSONB))
            updated = connection.execute(statement, values).mappings().one()
            return dict(updated)

    def _validate_binding(
        self, project_id: UUID, account_id: UUID | None, model: str | None, reasoning_effort: str | None
    ) -> None:
        if account_id is None:
            if reasoning_effort is not None:
                raise ValueError("reasoning effort requires a discovered model")
            return  # Preserve legacy free-text profile models.
        if model is None:
            raise ValueError("a provider account requires a catalog model")
        with self.engine.connect() as connection:
            catalog = connection.execute(
                text(
                    "SELECT a.auth_kind,a.provider,c.reasoning_efforts "
                    "FROM model_catalog c JOIN model_accounts a "
                    "ON a.project_id=c.project_id AND a.id=c.account_id "
                    "WHERE c.project_id = :project_id AND c.account_id = :account_id "
                    "AND c.model_id = :model AND a.catalog_valid=true "
                    "AND (a.auth_kind='none' OR a.credential_ready=true)"
                ),
                {"project_id": project_id, "account_id": account_id, "model": model},
            ).one_or_none()
        if catalog is None or (catalog.auth_kind == "api_key" and not self.secrets.present(account_id)):
            raise ValueError("model is not in this project's account catalog")
        if reasoning_effort is not None and (
            catalog.provider != "openai_codex_runtime" or reasoning_effort not in catalog.reasoning_efforts
        ):
            raise ValueError("reasoning effort is not supported by this account model")
