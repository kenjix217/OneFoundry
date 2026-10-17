"""Prove existing projects receive a versioned plan at migration time."""

import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from onefoundry.settings import Settings


def test_existing_projects_receive_empty_plan_and_saved_plan_blocks_downgrade() -> None:
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )
    engine = create_engine(settings.database_url)
    project_ids = [uuid4(), uuid4()]
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL search_path TO pg_temp, public"))
        connection.execute(text("CREATE TEMP TABLE projects (id uuid PRIMARY KEY, actor_id text NOT NULL)"))
        connection.execute(text("CREATE TEMP TABLE task_automation_decisions (id uuid PRIMARY KEY)"))
        connection.execute(
            text("INSERT INTO projects (id,actor_id) VALUES (:first,'operator'),(:second,'operator')"),
            {"first": project_ids[0], "second": project_ids[1]},
        )
        path = Path(__file__).parents[1] / "migrations" / "versions" / "0054_project_plan.py"
        spec = spec_from_file_location("project_plan_migration", path)
        assert spec is not None and spec.loader is not None
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.__dict__["op"] = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        plans = (
            connection.execute(
                text("SELECT project_id,body,version,actor_id FROM project_plans ORDER BY project_id")
            )
            .mappings()
            .all()
        )
        assert {row["project_id"] for row in plans} == set(project_ids)
        assert all(
            row["body"] == "" and row["version"] == 1 and row["actor_id"] == "operator" for row in plans
        )

        connection.execute(
            text("UPDATE project_plans SET body='Saved plan',version=2 WHERE project_id=:p"),
            {"p": project_ids[0]},
        )
        with pytest.raises(RuntimeError, match="lose them"):
            migration.downgrade()
        assert connection.scalar(text("SELECT count(*) FROM project_plans")) == 2
    engine.dispose()
