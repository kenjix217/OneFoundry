"""Exercise the installed-schema upgrade without using any live company data."""

from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from psycopg import connect, sql

from onefoundry.settings import Settings


def test_0059_upgrade_preserves_approved_fact_lineage_and_unknown_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = Settings()
    conninfo = bootstrap.database_url.set(drivername="postgresql").render_as_string(hide_password=False)
    database = f"memory_upgrade_fixture_{uuid4().hex}"
    with connect(conninfo, autocommit=True) as owner:
        owner.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        with monkeypatch.context() as environment:
            environment.setenv("DB_NAME", database)
            config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
            command.upgrade(config, "0059_discord_guild_channel")
            fixture_info = bootstrap.database_url.set(
                drivername="postgresql", database=database
            ).render_as_string(hide_password=False)
            project, profile, order, run, result, candidate, fact = (uuid4() for _ in range(7))
            unknown_order, unknown_run = uuid4(), uuid4()
            with connect(fixture_info) as connection:
                connection.execute(
                    "INSERT INTO projects (id,name,actor_id) VALUES (%s,'Memory upgrade fixture','operator')",
                    (project,),
                )
                connection.execute(
                    "INSERT INTO profiles (id,project_id,actor_id,name,kind,instructions,memory_scope,"
                    "allowed_tools,autonomy_level,configured) VALUES "
                    "(%s,%s,'operator','Chief of Staff','builtin','Fixture only','project','[]',"
                    "'supervised',false)",
                    (profile, project),
                )
                for order_id, run_id, status in (
                    (order, run, "done"),
                    (unknown_order, unknown_run, "unknown"),
                ):
                    connection.execute(
                        "INSERT INTO work_orders "
                        "(id,project_id,actor_id,title,instructions,profile_id,status) "
                        "VALUES (%s,%s,'operator','Fixture work','Fixture instructions',%s,%s)",
                        (order_id, project, profile, status),
                    )
                    connection.execute(
                        "INSERT INTO runs "
                        "(id,project_id,actor_id,work_order_id,profile_id,status,mode,finished_at) "
                        "VALUES (%s,%s,'operator',%s,%s,%s,'manual_planning',now())",
                        (run_id, project, order_id, profile, status),
                    )
                connection.execute(
                    "INSERT INTO run_results (id,project_id,actor_id,run_id,summary,scope) "
                    "VALUES (%s,%s,'operator',%s,'Copper approved fixture fact','project')",
                    (result, project, run),
                )
                connection.execute(
                    "INSERT INTO dreamcycle_candidates "
                    "(id,project_id,actor_id,source_result_id,summary,scope) "
                    "VALUES (%s,%s,'operator',%s,'Copper approved fixture fact','project')",
                    (candidate, project, result),
                )
                connection.execute(
                    "UPDATE dreamcycle_candidates SET status='approved', reviewed_at=now(), "
                    "reviewed_by_actor_id='operator' WHERE id=%s",
                    (candidate,),
                )
                connection.execute(
                    "INSERT INTO memory_items "
                    "(id,project_id,actor_id,scope,text,source_result_id,source_candidate_id) "
                    "VALUES (%s,%s,'operator','project','Copper approved fixture fact',%s,%s)",
                    (fact, project, result, candidate),
                )
                before = connection.execute(
                    "SELECT id,project_id,actor_id,scope,text,source_result_id,"
                    "source_candidate_id,created_at "
                    "FROM memory_items WHERE id=%s",
                    (fact,),
                ).fetchone()
                unknown_before = connection.execute(
                    "SELECT * FROM runs WHERE id=%s", (unknown_run,)
                ).fetchone()
            command.upgrade(config, "0071_company_memory")
            with connect(fixture_info) as connection:
                assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                    "0071_company_memory",
                )
                assert (
                    connection.execute(
                        "SELECT id,project_id,actor_id,scope,text,source_result_id,"
                        "source_candidate_id,created_at "
                        "FROM memory_items WHERE id=%s",
                        (fact,),
                    ).fetchone()
                    == before
                )
                assert connection.execute(
                    "SELECT state,superseded_by,changed_at,changed_by_actor_id FROM memory_items WHERE id=%s",
                    (fact,),
                ).fetchone() == ("current", None, None, None)
                assert (
                    connection.execute("SELECT * FROM runs WHERE id=%s", (unknown_run,)).fetchone()
                    == unknown_before
                )
                assert connection.execute("SELECT count(*) FROM memory_captures").fetchone() == (0,)
    finally:
        with connect(conninfo, autocommit=True) as owner:
            owner.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
