"""Prove the conversation-origin migration preserves historical Chief triage lineage."""

import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from onefoundry.settings import Settings


def test_historical_triage_turns_are_backfilled_before_origin_is_immutable() -> None:
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )
    engine = create_engine(settings.database_url)
    project_id = uuid4()
    triage_id, ordinary_id = uuid4(), uuid4()
    triage_turn, ordinary_turn = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL search_path TO pg_temp, public"))
        for statement in (
            "CREATE TEMP TABLE coordination_controls (project_id uuid PRIMARY KEY)",
            "CREATE TEMP TABLE task_conversations (id uuid PRIMARY KEY, project_id uuid NOT NULL, "
            "UNIQUE (project_id,id))",
            "CREATE TEMP TABLE task_conversation_turns (id uuid PRIMARY KEY, project_id uuid NOT NULL, "
            "conversation_id uuid NOT NULL, status text NOT NULL, UNIQUE (project_id,id))",
            "CREATE TEMP TABLE coordination_intents (project_id uuid NOT NULL, conversation_id uuid, "
            "action_kind text NOT NULL)",
            "CREATE TEMP TABLE remote_inbound (id uuid PRIMARY KEY, project_id uuid NOT NULL, "
            "UNIQUE (project_id,id))",
            "CREATE TEMP TABLE runs (id uuid PRIMARY KEY, project_id uuid NOT NULL, UNIQUE (project_id,id))",
            "CREATE TEMP TABLE coordination_rules (id uuid PRIMARY KEY, project_id uuid NOT NULL, "
            "UNIQUE (project_id,id))",
            "CREATE TEMP TABLE work_orders (id uuid PRIMARY KEY, project_id uuid NOT NULL, "
            "UNIQUE (project_id,id))",
            "CREATE TRIGGER task_conversation_turn_finalize BEFORE UPDATE ON task_conversation_turns "
            "FOR EACH ROW EXECUTE FUNCTION public.guard_task_conversation_turn()",
        ):
            connection.execute(text(statement))
        connection.execute(
            text("INSERT INTO task_conversations (id,project_id) VALUES (:triage,:p),(:ordinary,:p)"),
            {"p": project_id, "triage": triage_id, "ordinary": ordinary_id},
        )
        connection.execute(
            text(
                "INSERT INTO task_conversation_turns (id,project_id,conversation_id,status) "
                "VALUES (:triage_turn,:p,:triage,'done'),(:ordinary_turn,:p,:ordinary,'done')"
            ),
            {
                "p": project_id,
                "triage": triage_id,
                "ordinary": ordinary_id,
                "triage_turn": triage_turn,
                "ordinary_turn": ordinary_turn,
            },
        )
        connection.execute(
            text(
                "INSERT INTO coordination_intents (project_id,conversation_id,action_kind) "
                "VALUES (:p,:triage,'chief_triage')"
            ),
            {"p": project_id, "triage": triage_id},
        )

        path = Path(__file__).parents[1] / "migrations" / "versions" / "0053_conversation_origin.py"
        spec = spec_from_file_location("conversation_origin_migration", path)
        assert spec is not None and spec.loader is not None
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.__dict__["op"] = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        rows = connection.execute(
            text("SELECT id,source_kind FROM task_conversation_turns ORDER BY id")
        ).all()
        assert dict(rows) == {triage_turn: "system_triage", ordinary_turn: "operator_ui"}
        enabled = connection.scalar(
            text(
                "SELECT tgenabled FROM pg_trigger "
                "WHERE tgrelid='task_conversation_turns'::regclass "
                "AND tgname='task_conversation_turn_finalize'"
            )
        )
        assert enabled == "O"
    engine.dispose()
