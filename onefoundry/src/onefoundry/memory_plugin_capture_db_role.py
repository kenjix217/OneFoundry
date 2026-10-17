"""Provision a new draft-only login; existing readers and fact publishers stay unchanged."""

from psycopg import connect, sql

from onefoundry.db_roles import RuntimeRoleSettings
from onefoundry.memory_plugin_capture_settings import MemoryCaptureDatabaseSettings
from onefoundry.memory_plugin_settings import MemoryPluginSettings
from onefoundry.settings import Settings


def provision_memory_capture_role(
    bootstrap: Settings, runtime: RuntimeRoleSettings, *, deadline_seconds: int
) -> None:
    if runtime.app_db_user == bootstrap.db_user:
        raise ValueError("capture login must differ from the owner")
    if type(deadline_seconds) is not int or not 2 <= deadline_seconds <= 60:
        raise ValueError("invalid capture DB deadline")
    role = sql.Identifier(runtime.app_db_user)
    with connect(
        bootstrap.database_url.set(drivername="postgresql").render_as_string(hide_password=False),
        connect_timeout=deadline_seconds,
        options=f"-c statement_timeout={deadline_seconds * 1000} -c lock_timeout={deadline_seconds * 1000}",
    ) as connection:
        lock_function = connection.execute(
            "SELECT to_regprocedure('public.memory_capture_lock_project(uuid)')"
        ).fetchone()
        if lock_function is None or lock_function[0] is None:
            raise ValueError("capture lock migration must be installed first")
        if connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (runtime.app_db_user,)).fetchone():
            raise ValueError("capture login already exists; never repurpose an existing reader")
        if connection.execute(
            "SELECT 1 FROM information_schema.table_privileges WHERE grantee='PUBLIC' AND "
            "table_schema='public' LIMIT 1"
        ).fetchone():
            raise ValueError("PUBLIC table grants require review")
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                "NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(role, sql.Literal(runtime.app_db_password))
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(bootstrap.db_name), role)
        )
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        connection.execute(
            sql.SQL(
                "GRANT SELECT ON TABLE projects, memory_items, run_results, dreamcycle_candidates TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT SELECT (id, project_id, actor_id, client_capture_id, payload_sha256, "
                "source_sha256) ON "
                "memory_captures TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT INSERT (id, project_id, actor_id, client_capture_id, source_ref, "
                "messages, source_sha256, payload_sha256) ON memory_captures TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT INSERT (id, project_id, actor_id, summary, scope, source_kind, "
                "source_capture_id, source_sha256) ON run_results TO {}"
            ).format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT INSERT (id, project_id, actor_id, source_result_id, summary, scope, "
                "replaces_memory_id) ON dreamcycle_candidates TO {}"
            ).format(role)
        )
        connection.execute(sql.SQL("GRANT INSERT ON TABLE events TO {}").format(role))
        connection.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION public.memory_capture_lock_project(uuid) TO {}").format(role)
        )


def main() -> None:
    try:
        capture = MemoryCaptureDatabaseSettings()  # type: ignore[call-arg]
        provision_memory_capture_role(
            Settings(),
            RuntimeRoleSettings(app_db_user=capture.db_user, app_db_password=capture.db_password),
            deadline_seconds=MemoryPluginSettings().deadline_seconds,  # type: ignore[call-arg]
        )
    except Exception:
        raise SystemExit(
            "memory_capture_role_provision_failed: check migration, owner access and a new private login"
        ) from None


if __name__ == "__main__":
    main()
