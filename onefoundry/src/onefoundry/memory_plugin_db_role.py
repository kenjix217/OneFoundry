"""One-time, transactional provisioning for the read-only memory MCP facade."""

from psycopg import connect, sql

from onefoundry.db_roles import RuntimeRoleSettings
from onefoundry.memory_plugin_settings import MemoryPluginSettings
from onefoundry.settings import Settings


def provision_memory_read_role(
    bootstrap: Settings, runtime: RuntimeRoleSettings, *, deadline_seconds: int
) -> None:
    """Create a new dedicated login; never repurpose or alter an existing role."""
    if runtime.app_db_user.casefold() == bootstrap.db_user.casefold():
        raise ValueError("memory DB login must differ from the bootstrap login")
    if type(deadline_seconds) is not int or not 2 <= deadline_seconds <= 60:
        raise ValueError("memory DB deadline must be between 2 and 60 seconds")
    conninfo = bootstrap.database_url.set(drivername="postgresql").render_as_string(hide_password=False)
    role = sql.Identifier(runtime.app_db_user)
    with connect(
        conninfo,
        connect_timeout=deadline_seconds,
        options=f"-c statement_timeout={deadline_seconds * 1000} -c lock_timeout={deadline_seconds * 1000}",
    ) as connection:
        # Check the actual lifecycle contract before creating credentials or grants.
        connection.execute(
            "SELECT state, superseded_by, changed_at, changed_by_actor_id FROM memory_items LIMIT 0"
        )
        if connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (runtime.app_db_user,)).fetchone():
            raise ValueError("memory DB login already exists; choose a new dedicated identity")
        if connection.execute(
            "SELECT 1 FROM information_schema.table_privileges "
            "WHERE grantee='PUBLIC' AND table_schema='public' LIMIT 1"
        ).fetchone():
            raise ValueError("PUBLIC table privileges would widen memory access; review them first")
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
        connection.execute(sql.SQL("GRANT SELECT ON TABLE projects, memory_items TO {}").format(role))
        connection.execute(sql.SQL("GRANT SELECT ON TABLE memory_embeddings TO {}").format(role))
        # ToolGateway owns receipts; no event read/update/delete or sequence grants.
        connection.execute(sql.SQL("GRANT INSERT ON TABLE events TO {}").format(role))


def main() -> None:
    try:
        provision_memory_read_role(
            Settings(),
            RuntimeRoleSettings(),
            deadline_seconds=MemoryPluginSettings().deadline_seconds,  # type: ignore[call-arg]
        )
    except Exception:
        raise SystemExit(
            "memory_read_role_provision_failed: check schema, owner access and a new dedicated login"
        ) from None


if __name__ == "__main__":
    main()
