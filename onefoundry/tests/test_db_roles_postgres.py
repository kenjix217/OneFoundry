"""Runtime role reprovisioning must keep existing readers authorized."""

from __future__ import annotations

import secrets
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import pytest
from psycopg import connect as pg_connect
from psycopg import sql

from onefoundry import db_roles
from onefoundry.db_roles import RuntimeRoleSettings, provision_runtime_role
from onefoundry.settings import Settings


def _connection_info(settings: Settings) -> str:
    return settings.database_url.set(drivername="postgresql").render_as_string(hide_password=False)


def _can_read_input_files(settings: Settings, role_name: str) -> bool:
    with pg_connect(_connection_info(settings)) as connection:
        row = connection.execute(
            "SELECT has_table_privilege(%s, 'public.input_files', 'SELECT')", (role_name,)
        ).fetchone()
        assert row is not None
        return bool(row[0])


def _role_is_unprivileged(settings: Settings, role_name: str) -> bool:
    with pg_connect(_connection_info(settings)) as connection:
        row = connection.execute(
            "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
            "FROM pg_roles WHERE rolname=%s",
            (role_name,),
        ).fetchone()
        return row == (False, False, False, False, False)


def _can_claim_input_files(settings: Settings, role_name: str) -> bool:
    with pg_connect(_connection_info(settings)) as connection:
        connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role_name)))
        connection.execute(
            "SELECT 1 FROM input_files WHERE status='pending' "
            "ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        return True


def _drop_test_role(settings: Settings, role_name: str) -> None:
    role = sql.Identifier(role_name)
    with pg_connect(_connection_info(settings), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP OWNED BY {}").format(role))
        connection.execute(sql.SQL("DROP ROLE {}").format(role))


class _PauseAfterRevoke:
    def __init__(self, connection: Any, paused: Event, resume: Event) -> None:
        self.connection = connection
        self.paused = paused
        self.resume = resume

    def __enter__(self) -> _PauseAfterRevoke:
        self.connection.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self.connection.__exit__(*args)

    def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        result = self.connection.execute(query, *args, **kwargs)
        statement = query.as_string(self.connection) if isinstance(query, sql.Composable) else query
        if statement.startswith("REVOKE ALL ON TABLE"):
            self.paused.set()
            if not self.resume.wait(10):
                raise TimeoutError("reprovision test did not release revoked grants")
        return result


def test_existing_reader_keeps_grants_during_reprovision(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = Settings()
    runtime = RuntimeRoleSettings(
        app_db_user=f"role_regrant_{uuid4().hex}", app_db_password=secrets.token_urlsafe(32)
    )
    provision_runtime_role(bootstrap, runtime)
    assert _role_is_unprivileged(bootstrap, runtime.app_db_user)
    assert _can_claim_input_files(bootstrap, runtime.app_db_user)
    paused, resume = Event(), Event()
    failures: list[BaseException] = []

    def connect_paused(*args: Any, **kwargs: Any) -> _PauseAfterRevoke:
        return _PauseAfterRevoke(pg_connect(*args, **kwargs), paused, resume)

    def reprovision() -> None:
        try:
            provision_runtime_role(bootstrap, runtime)
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(db_roles, "connect", connect_paused)
    worker = Thread(target=reprovision, daemon=True)
    try:
        worker.start()
        assert paused.wait(10), "reprovision did not reach its privilege transition"
        assert _can_read_input_files(bootstrap, runtime.app_db_user)
    finally:
        resume.set()
        worker.join(15)
        _drop_test_role(bootstrap, runtime.app_db_user)
    assert not worker.is_alive()
    assert not failures


def test_failed_reprovision_preserves_existing_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = Settings()
    runtime = RuntimeRoleSettings(
        app_db_user=f"role_regrant_{uuid4().hex}", app_db_password=secrets.token_urlsafe(32)
    )
    provision_runtime_role(bootstrap, runtime)
    assert _role_is_unprivileged(bootstrap, runtime.app_db_user)
    original_connect = pg_connect

    class FailAfterRevoke:
        def __init__(self, connection: Any) -> None:
            self.connection = connection

        def __enter__(self) -> FailAfterRevoke:
            self.connection.__enter__()
            return self

        def __exit__(self, *args: Any) -> Any:
            return self.connection.__exit__(*args)

        def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
            result = self.connection.execute(query, *args, **kwargs)
            statement = query.as_string(self.connection) if isinstance(query, sql.Composable) else query
            if statement.startswith("REVOKE ALL ON TABLE"):
                raise RuntimeError("injected grant failure")
            return result

    monkeypatch.setattr(
        db_roles, "connect", lambda *args, **kwargs: FailAfterRevoke(original_connect(*args, **kwargs))
    )
    try:
        with pytest.raises(RuntimeError, match="injected grant failure"):
            provision_runtime_role(bootstrap, runtime)
        assert _can_read_input_files(bootstrap, runtime.app_db_user)
        assert _can_claim_input_files(bootstrap, runtime.app_db_user)
        assert _role_is_unprivileged(bootstrap, runtime.app_db_user)
    finally:
        _drop_test_role(bootstrap, runtime.app_db_user)
