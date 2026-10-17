"""Prove dedicated-login retrieval and denied writes against real PostgreSQL."""

import hashlib
import json
import secrets
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import connect, sql
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.db_roles import RuntimeRoleSettings
from onefoundry.main import create_app
from onefoundry.memory_plugin_api_key import MemoryApiKeyVerifier
from onefoundry.memory_plugin_auth import MemoryTokenVerifier
from onefoundry.memory_plugin_db_role import provision_memory_read_role
from onefoundry.memory_plugin_gateway import MemoryPluginGateway
from onefoundry.memory_plugin_service import create_memory_plugin_app
from onefoundry.memory_plugin_settings import MemoryGrant
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.memory_plugin_fixtures import keypair, policy, token
from tests.test_company_memory_postgres import capture, owner, publish
from tests.test_task_session_postgres import _settings


@pytest.fixture
def read_login() -> Iterator[RuntimeRoleSettings]:
    bootstrap = Settings()
    runtime = RuntimeRoleSettings(
        app_db_user=f"memory_fixture_{uuid4().hex}", app_db_password=secrets.token_urlsafe(32)
    )
    provision_memory_read_role(bootstrap, runtime, deadline_seconds=10)
    try:
        yield runtime
    finally:
        conninfo = bootstrap.database_url.set(drivername="postgresql").render_as_string(hide_password=False)
        with connect(conninfo, autocommit=True) as connection:
            role = sql.Identifier(runtime.app_db_user)
            connection.execute(sql.SQL("DROP OWNED BY {}").format(role))
            connection.execute(sql.SQL("DROP ROLE {}").format(role))


@pytest.mark.parametrize("auth_mode", ["oauth", "api_key"])
def test_dedicated_login_has_only_fact_reads_and_gateway_receipts(
    read_login: RuntimeRoleSettings, tmp_path: Path, auth_mode: str
) -> None:
    operator = owner()
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as local:
        project = UUID(
            local.post(
                "/api/projects",
                json={"name": "Memory role fixture"},
                headers={"origin": "http://localhost:8765"},
            ).json()["id"]
        )
        fact = publish(operator, project, capture("Copper bounded memory fixture"))
    engine = create_engine(
        Settings(db_user=read_login.app_db_user, db_password=read_login.app_db_password).database_url,
        hide_parameters=True,
    )
    try:
        grant = MemoryGrant(
            subject="synthetic-operator",
            client_id="synthetic-codex",
            actor_id="external-memory:codex",
            project_ids=(project,),
        )
        configured = policy(grants=(grant,))
        private, public = keypair()
        verifier: MemoryTokenVerifier | MemoryApiKeyVerifier = MemoryTokenVerifier(configured, public)
        bearer = token(private, configured)
        if auth_mode == "api_key":
            key_path = tmp_path / "key-records.json"
            configured = policy(
                grants=(grant,),
                auth_mode="api_key",
                api_key_records_path=key_path,
                oauth_issuer=None,
                oauth_public_key_path=None,
                public_url="http://127.0.0.1:9877/mcp",
            )
            bearer = secrets.token_urlsafe(32)
            key_path.write_text(
                json.dumps(
                    [
                        {
                            "sha256": hashlib.sha256(bearer.encode()).hexdigest(),
                            "subject": grant.subject,
                            "client_id": grant.client_id,
                            "expires_at": int(time.time()) + 3600,
                        }
                    ]
                )
            )
            key_path.chmod(0o600)
            verifier = MemoryApiKeyVerifier(configured)
        gateway = MemoryPluginGateway(configured, {grant.actor_id: ToolGateway(engine, grant.actor_id)})
        app = create_memory_plugin_app(configured, gateway, verifier)
        with TestClient(
            app,
            base_url=configured.public_url.removesuffix("/mcp"),
            headers={
                "authorization": f"Bearer {bearer}",
                "accept": "application/json, text/event-stream",
            },
        ) as remote:
            result = remote.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "memory_search",
                        "arguments": {"project_id": str(project), "query": "Copper"},
                    },
                },
            ).json()["result"]["structuredContent"]
            assert result["items"][0]["id"] == str(fact["id"])
        with engine.connect() as connection:
            privileges = connection.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema.table_privileges "
                    "WHERE grantee=current_user ORDER BY table_name, privilege_type"
                )
            ).all()
            assert privileges == [
                ("events", "INSERT"),
                ("memory_embeddings", "SELECT"),
                ("memory_items", "SELECT"),
                ("projects", "SELECT"),
            ]
            assert not connection.scalar(
                text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_auth_members WHERE member=("
                        "SELECT oid FROM pg_roles WHERE rolname=current_user)"
                    )
                )
                == 0
            )
        for statement in (
            "SELECT messages FROM memory_captures LIMIT 1",
            "SELECT instructions FROM profiles LIMIT 1",
            "SELECT payload FROM events LIMIT 1",
            "SELECT summary FROM dreamcycle_candidates LIMIT 1",
            "UPDATE memory_items SET state='withdrawn' WHERE false",
            "DELETE FROM memory_items WHERE false",
            "UPDATE events SET payload='{}'::jsonb WHERE false",
            "DELETE FROM events WHERE false",
            "CREATE TABLE public.memory_forbidden (id int)",
        ):
            with engine.begin() as connection, pytest.raises(DBAPIError):
                connection.execute(text(statement))
        with operator.engine.connect() as connection:
            receipts = cast(
                Sequence[dict[str, Any]],
                connection.execute(
                    text("SELECT payload FROM events WHERE project_id=:p AND actor_id=:actor"),
                    {"p": project, "actor": grant.actor_id},
                )
                .scalars()
                .all(),
            )
            assert {receipt["phase"] for receipt in receipts} == {"started", "finished"}
            assert "Copper" not in str(receipts)
    finally:
        engine.dispose()
        operator.engine.dispose()


def test_existing_login_is_never_altered(read_login: RuntimeRoleSettings) -> None:
    with pytest.raises(ValueError, match="already exists"):
        provision_memory_read_role(Settings(), read_login, deadline_seconds=10)
    # The original login still authenticates after the refused reprovision.
    engine = create_engine(
        Settings(db_user=read_login.app_db_user, db_password=read_login.app_db_password).database_url,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == read_login.app_db_user
    finally:
        engine.dispose()
