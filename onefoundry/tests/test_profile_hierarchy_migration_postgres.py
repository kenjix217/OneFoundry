"""Run the hierarchy migration against temporary legacy tables in PostgreSQL."""

import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from onefoundry.settings import Settings


def _migration() -> ModuleType:
    path = Path(__file__).parents[1] / "migrations" / "versions" / "0028_profile_hierarchy.py"
    spec = spec_from_file_location("profile_hierarchy_migration", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("existing_name", "reuse_as_root"),
    [("Chief of Staff", True), ("chief of staff", True), ("chief of ſtaff", False)],
)
def test_existing_chief_name_is_resolved_without_changing_custom_profile(
    existing_name: str, reuse_as_root: bool
) -> None:
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )
    engine = create_engine(settings.database_url)
    project_id, other_project_id = uuid4(), uuid4()
    chief_id, engineer_id, qa_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TEMP TABLE projects (id uuid PRIMARY KEY, actor_id text NOT NULL) ON COMMIT DROP")
        )
        connection.execute(
            text(
                "CREATE TEMP TABLE profiles (id uuid PRIMARY KEY, project_id uuid NOT NULL, "
                "actor_id text NOT NULL, name text NOT NULL, kind text NOT NULL, model text, "
                "instructions text NOT NULL, memory_scope text NOT NULL, allowed_tools jsonb NOT NULL, "
                "autonomy_level text NOT NULL, configured boolean NOT NULL, "
                "UNIQUE (project_id, id)) ON COMMIT DROP"
            )
        )
        connection.execute(
            text("CREATE UNIQUE INDEX profiles_legacy_name_uq ON profiles (project_id, lower(name))")
        )
        connection.execute(
            text("INSERT INTO projects (id, actor_id) VALUES (:first, 'operator'), (:second, 'operator')"),
            {"first": project_id, "second": other_project_id},
        )
        connection.execute(
            text(
                "INSERT INTO profiles (id, project_id, actor_id, name, kind, model, instructions, "
                "memory_scope, allowed_tools, autonomy_level, configured) VALUES "
                "(:id, :project_id, 'operator', :name, :kind, :model, :instructions, "
                "'project', '[\"search_read\"]'::jsonb, 'supervised', true)"
            ),
            [
                {
                    "id": chief_id,
                    "project_id": project_id,
                    "name": existing_name,
                    "kind": "custom",
                    "model": "existing-model",
                    "instructions": "Existing operator instructions",
                },
                {
                    "id": engineer_id,
                    "project_id": project_id,
                    "name": "Engineer",
                    "kind": "builtin",
                    "model": "existing-model",
                    "instructions": "Engineer instructions",
                },
                {
                    "id": qa_id,
                    "project_id": project_id,
                    "name": "QA",
                    "kind": "builtin",
                    "model": "existing-model",
                    "instructions": "QA instructions",
                },
            ],
        )

        migration = _migration()
        migration.__dict__["op"] = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        first_rows = (
            connection.execute(
                text(
                    "SELECT id, name, kind, model, instructions, memory_scope, allowed_tools, "
                    "autonomy_level, reports_to_profile_id FROM profiles WHERE project_id=:id ORDER BY name"
                ),
                {"id": project_id},
            )
            .mappings()
            .all()
        )
        assert len(first_rows) == (3 if reuse_as_root else 4)
        saved = {row["id"]: row for row in first_rows}
        assert saved[chief_id]["name"] == existing_name
        assert saved[chief_id]["kind"] == "custom"
        assert saved[chief_id]["model"] == "existing-model"
        assert saved[chief_id]["instructions"] == "Existing operator instructions"
        assert saved[chief_id]["memory_scope"] == "project"
        assert saved[chief_id]["allowed_tools"] == ["search_read"]
        assert saved[chief_id]["autonomy_level"] == "supervised"
        root_id = (
            chief_id
            if reuse_as_root
            else next(row["id"] for row in first_rows if row["name"] == "Chief of Staff")
        )
        assert saved[root_id]["reports_to_profile_id"] is None
        if reuse_as_root:
            assert saved[chief_id]["reports_to_profile_id"] is None
        else:
            assert saved[root_id]["kind"] == "builtin"
            assert saved[chief_id]["reports_to_profile_id"] == root_id
        assert saved[engineer_id]["reports_to_profile_id"] == root_id
        assert saved[qa_id]["reports_to_profile_id"] == root_id
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("UPDATE profiles SET reports_to_profile_id=NULL WHERE id=:id"),
                {"id": qa_id},
            )
        other_rows = (
            connection.execute(
                text("SELECT name, kind, reports_to_profile_id FROM profiles WHERE project_id=:id"),
                {"id": other_project_id},
            )
            .mappings()
            .all()
        )
        assert len(other_rows) == 1
        assert (other_rows[0]["name"], other_rows[0]["kind"], other_rows[0]["reports_to_profile_id"]) == (
            "Chief of Staff",
            "builtin",
            None,
        )
    engine.dispose()
