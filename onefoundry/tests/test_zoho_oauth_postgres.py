"""Durable sign-in claims, project/version isolation and refresh serialization."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.main import create_app
from onefoundry.tool_gateway import ToolGateway
from onefoundry.zoho_mail_contracts import ZohoEndpoint, ZohoSetup
from onefoundry.zoho_mail_mcp import ZohoMcpError
from onefoundry.zoho_mail_oauth import ZohoOAuth
from onefoundry.zoho_mail_signin import ZohoSignInStore
from onefoundry.zoho_mail_store import ZohoMailStore
from tests.test_smtp_postgres import _post
from tests.test_zoho_mail_postgres import Provider, _settings
from tests.test_zoho_oauth_unit import SERVER, OAuthFixture


def setup_signin(
    tmp_path: Path,
) -> tuple[ZohoMailStore, ZohoSignInStore, OAuthFixture, dict[str, Any], list[float]]:
    settings = _settings(tmp_path)
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    clock = [1000.0]
    provider = Provider()
    store = ZohoMailStore(gateway, settings, lambda *_: provider, clock=lambda: clock[0])
    store.prepare_secrets()
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = _post(client, "/api/projects", {"name": f"Zoho sign-in {uuid4()}"})
    pid = UUID(project["id"])
    row = store.create(pid, ZohoSetup(label="Zoho Mail"))
    fixture = OAuthFixture()
    signin = ZohoSignInStore(store, ZohoOAuth(fixture, ("mcp.zoho.com",)))
    signin.endpoint(
        pid, row["id"], ZohoEndpoint(expected_version=row["version"], server_url=SecretStr(SERVER))
    )
    with engine.connect() as connection:
        return store, signin, fixture, store.row(connection, pid, row["id"]), clock


def test_callback_is_single_use_and_tokens_never_enter_public_rows_or_events(tmp_path: Path) -> None:
    store, signin, fixture, row, _ = setup_signin(tmp_path)
    pid, cid = row["project_id"], row["id"]
    try:
        result = signin.start(pid, cid, row["version"])
        assert result == {
            "authorize_path": f"/api/projects/{pid}/mail/zoho/connections/{cid}/oauth/authorize"
        }
        with store.gateway.engine.connect() as connection:
            current = store.row(connection, pid, cid)
        value = store.protected(current)
        assert store.public(current)["oauth_pending"] and not store.public(current)["credential_present"]
        assert "code_challenge=" in signin.authorize(pid, cid)
        state = value["pending"]["state"]

        def finish() -> str:
            try:
                saved = signin.callback(pid, cid, state, "one-use-code")
                assert saved["status"] == "ready" and not saved["enabled"]
                return "connected"
            except Conflict:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: finish(), range(2)))
        assert sorted(outcomes) == ["connected", "rejected"]
        assert sum(url.endswith("/token") for _, url, _ in fixture.calls) == 1
        with store.gateway.engine.connect() as connection:
            saved = store.row(connection, pid, cid)
            events = str(
                connection.execute(text("SELECT payload FROM events WHERE project_id=:p"), {"p": pid}).all()
            )
        visible = json.dumps(store.public(saved), default=str)
        for private in (SERVER, "synthetic-access", "synthetic-refresh", "one-use-code", state):
            assert private not in visible and private not in events
        assert store.protected(saved)["refresh_token"] == "synthetic-refresh"
        assert "pending" not in store.protected(saved)
    finally:
        store.gateway.engine.dispose()


def test_refresh_is_serialized_and_preserves_approval_identity(tmp_path: Path) -> None:
    store, signin, fixture, row, clock = setup_signin(tmp_path)
    pid, cid = row["project_id"], row["id"]
    try:
        signin.start(pid, cid, row["version"])
        with store.gateway.engine.connect() as connection:
            waiting = store.row(connection, pid, cid)
        state = store.protected(waiting)["pending"]["state"]
        signin.callback(pid, cid, state, "one-use-code")
        with store.gateway.engine.connect() as connection:
            signed = store.row(connection, pid, cid)
        enabled = store.set_enabled(pid, cid, True, signed["version"])
        with store.gateway.engine.connect() as connection:
            caller = store.row(connection, pid, cid)
        clock[0] += 61
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: signin.access(caller), range(2)))
        assert all(result["access_token"] == "synthetic-access" for result in results)
        assert (
            sum(
                body is not None and body.get("grant_type") == "refresh_token" for _, _, body in fixture.calls
            )
            == 1
        )
        with store.gateway.engine.connect() as connection:
            refreshed = store.row(connection, pid, cid)
        assert refreshed["version"] == enabled["version"] and refreshed["enabled"]
        assert refreshed["credential_revision"] == caller["credential_revision"] + 1
        fixture.reject = True
        clock[0] += 61
        with pytest.raises(ZohoMcpError, match="authentication_failed"):
            signin.access(refreshed)
        assert not store.list(pid)[0]["enabled"]
    finally:
        store.gateway.engine.dispose()


def test_wrong_project_state_denial_expiry_and_endpoint_change_fail_closed(tmp_path: Path) -> None:
    store, signin, _, row, clock = setup_signin(tmp_path)
    pid, cid = row["project_id"], row["id"]
    try:
        with pytest.raises(NotFound):
            signin.start(uuid4(), cid, row["version"])
        signin.start(pid, cid, row["version"])
        with store.gateway.engine.connect() as connection:
            current = store.row(connection, pid, cid)
        state = store.protected(current)["pending"]["state"]
        with pytest.raises(Conflict):
            signin.callback(pid, cid, "wrong-state", "one-use-code")
        with pytest.raises(Conflict):
            signin.callback(pid, cid, state, None, denied=True)
        with pytest.raises(Conflict):
            signin.callback(pid, cid, state, "one-use-code")
        current = store.list(pid)[0]
        signin.start(pid, cid, current["version"])
        with store.gateway.engine.connect() as connection:
            waiting = store.row(connection, pid, cid)
        state = store.protected(waiting)["pending"]["state"]
        clock[0] += store.settings.zoho_oauth_pending_timeout_seconds + 1
        with pytest.raises(Conflict):
            signin.callback(pid, cid, state, "one-use-code")
        signin.endpoint(
            pid, cid, ZohoEndpoint(expected_version=waiting["version"], server_url=SecretStr(SERVER))
        )
        with pytest.raises(Conflict):
            signin.callback(pid, cid, state, "one-use-code")
        assert not store.list(pid)[0]["credential_present"]
    finally:
        store.gateway.engine.dispose()


def test_missing_credential_commit_revision_and_legacy_manual_format(tmp_path: Path) -> None:
    store, _, _, row, _ = setup_signin(tmp_path)
    try:
        value = store.protected(row)
        value["credential_revision"] += 1
        store.secrets.write(row["id"], base64.urlsafe_b64encode(json.dumps(value).encode()).decode())
        assert not store.public(row)["endpoint_present"]
        legacy = {
            "version": row["version"],
            "server_url": SERVER,
            "access_token": "legacy-synthetic",
            "mail_account_id": "",
        }
        with store.gateway.engine.begin() as connection:
            connection.execute(
                text("UPDATE zoho_mail_connections SET credential_revision=1 WHERE id=:id"), {"id": row["id"]}
            )
            fresh = store.row(connection, row["project_id"], row["id"])
        store.secrets.write(row["id"], base64.urlsafe_b64encode(json.dumps(legacy).encode()).decode())
        assert store.vault(fresh)["access_token"] == "legacy-synthetic"
    finally:
        store.gateway.engine.dispose()


def test_replacement_during_exchange_does_not_install_old_tokens(tmp_path: Path) -> None:
    store, signin, fixture, row, _ = setup_signin(tmp_path)
    pid, cid = row["project_id"], row["id"]
    entered, release = Event(), Event()

    def blocked(
        method: str, url: str, *, body: dict[str, Any] | None = None, form: bool = False
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if url.endswith("/token"):
            entered.set()
            assert release.wait(10), "fixture exchange release missing"
        return fixture(method, url, body=body, form=form)

    signin.oauth = ZohoOAuth(blocked, ("mcp.zoho.com",))
    try:
        signin.start(pid, cid, row["version"])
        with store.gateway.engine.connect() as connection:
            waiting = store.row(connection, pid, cid)
        state = store.protected(waiting)["pending"]["state"]
        with pytest.raises(Conflict):
            signin.callback(pid, cid, state, "code", issuer="https://foreign.example")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(signin.callback, pid, cid, state, "code")
            try:
                assert entered.wait(10), "fixture exchange did not start"
                changed = signin.endpoint(
                    pid, cid, ZohoEndpoint(expected_version=waiting["version"], server_url=SecretStr(SERVER))
                )
            finally:
                release.set()
            with pytest.raises(Conflict):
                future.result(timeout=10)
        saved = store.list(pid)[0]
        assert saved["version"] == changed["version"] and not saved["credential_present"]
        with store.gateway.engine.begin() as connection:
            connection.execute(text("UPDATE projects SET status='archived' WHERE id=:p"), {"p": pid})
        with pytest.raises(Conflict):
            signin.start(pid, cid, saved["version"])
    finally:
        release.set()
        store.gateway.engine.dispose()


def test_migration_preserves_existing_connections_and_blocks_unsafe_downgrade(tmp_path: Path) -> None:
    engine = create_engine(_settings(tmp_path).database_url)
    path = Path(__file__).parents[1] / "migrations/versions/0084_zoho_oauth_revision.py"
    spec = spec_from_file_location("zoho_revision_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TEMP TABLE zoho_mail_connections (id uuid, label text, version integer, "
                    "enabled boolean) ON COMMIT DROP"
                )
            )
            cid = uuid4()
            connection.execute(
                text("INSERT INTO zoho_mail_connections VALUES (:id,'Existing mailbox',7,false)"), {"id": cid}
            )
            migration.__dict__["op"] = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            saved = connection.execute(text("SELECT * FROM zoho_mail_connections")).mappings().one()
            assert dict(saved) == {
                "id": cid,
                "label": "Existing mailbox",
                "version": 7,
                "enabled": False,
                "credential_revision": 1,
            }
            with pytest.raises(RuntimeError, match="checkpoint"):
                migration.downgrade()
            connection.execute(text("DELETE FROM zoho_mail_connections"))
            migration.downgrade()
            assert (
                "credential_revision"
                not in connection.execute(text("SELECT * FROM zoho_mail_connections")).keys()
            )
    finally:
        engine.dispose()
