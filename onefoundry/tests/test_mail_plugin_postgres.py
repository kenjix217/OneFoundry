"""Synthetic TLS mailbox reads with real project scoping and append-only PostgreSQL receipts."""

import base64
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.mail_imap import ImapMailbox, MailConfig, MailError
from onefoundry.mail_plugin_contracts import MessageIds
from onefoundry.mail_plugin_gateway import MailPluginGateway, load_message_ids
from onefoundry.mail_plugin_imap import SearchMailbox
from onefoundry.mail_plugin_settings import MailPluginSettings
from onefoundry.mail_schemas import MailAccountCreate
from onefoundry.mail_store import MailStore
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.projects import ProjectStore
from onefoundry.schemas import ProjectCreate
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.fake_imap import message
from tests.fake_mail_plugin_imap import PluginMailbox, serve
from tests.mail_plugin_fixtures import policy


def test_project_bound_read_receipts_scope_denials_and_existing_secret_reuse(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    engine = create_engine(settings.database_url, hide_parameters=True)
    operator = ToolGateway(engine, "synthetic-operator", live_text_settings=settings)
    projects = ProjectStore(engine, operator.actor_id, ModelSecretStore(str(root)))
    project_id = UUID(str(projects.create_project(ProjectCreate(name=f"Mail plugin {uuid4()}"))["id"]))
    other_id = UUID(str(projects.create_project(ProjectCreate(name=f"Other mail {uuid4()}"))["id"]))
    fixture = PluginMailbox(
        messages={101: message("Synthetic pricing", "buyer@example.test", "Synthetic mail body")}
    )
    store = MailStore(operator, settings)
    store.prepare_secrets()
    try:
        with serve(fixture, tmp_path) as (port, context):
            account = store.create(
                project_id,
                MailAccountCreate(
                    label="Synthetic plugin inbox",
                    host="127.0.0.1",
                    port=port,
                    username=fixture.username,
                    password=SecretStr(fixture.password),
                ),
            )
            account_id = UUID(str(account["id"]))
            store.mailbox_factory = lambda config: ImapMailbox(config, context)
            assert store.probe(project_id, account_id)["status"] == "ready"
            configured = policy(
                project_id=project_id, account_id=account_id, imap_port=port, folders=("INBOX",)
            )
            key_root = root / "mail-plugin"
            key_root.mkdir(mode=0o700)
            # Synthetic fixture key only, never a live key or a chat handoff credential.
            ModelSecretStore(str(key_root)).write(
                configured.id_key_id,
                base64.urlsafe_b64encode(b"synthetic-signing-key-at-least-32-bytes").decode(),
            )
            ids = load_message_ids(store, configured)

            def factory(cfg: MailConfig, pol: MailPluginSettings, identifiers: MessageIds) -> SearchMailbox:
                return SearchMailbox(cfg, pol, identifiers, context=context)

            owner = ToolGateway(engine, configured.actor_id, live_text_settings=settings)
            plugin = MailPluginGateway(owner, store, configured, ids, factory=factory)
            plugin.check_ready()
            found = plugin.search({"folder": "INBOX", "sender": "buyer@"})
            identifier = found["messages"][0]["message_id"]
            assert plugin.read({"message_id": identifier})["text"] == "Synthetic mail body"
            with pytest.raises(MailError, match="folder_denied"):
                plugin.search({"folder": "Trash"})
            with pytest.raises(MailError, match="invalid_input"):
                plugin.search({"folder": "INBOX", "password": "synthetic-rejected-secret"})
            before = len(fixture.commands)
            other = configured.model_copy(update={"project_id": other_id})
            other_ids = MessageIds(ids.key, other_id, account_id)
            wrong_scope = MailPluginGateway(owner, store, other, other_ids, factory=factory)
            with pytest.raises(MailError, match="account_unavailable"):
                wrong_scope.search({"folder": "INBOX"})
            # An altered pinned binding fails without broadening the DB role's privileges.
            changed = configured.model_copy(update={"imap_host": "changed.example.test"})
            wrong_binding = MailPluginGateway(owner, store, changed, ids, factory=factory)
            with pytest.raises(MailError, match="account_binding_denied"):
                wrong_binding.search({"folder": "INBOX"})
            assert len(fixture.commands) == before
            with engine.connect() as connection:
                row = (
                    connection.execute(
                        text("SELECT enabled,last_poll_at,last_uid FROM mail_accounts WHERE id=:id"),
                        {"id": account_id},
                    )
                    .mappings()
                    .one()
                )
                assert row["enabled"] is False and row["last_poll_at"] is None
                receipts = (
                    connection.execute(
                        text(
                            "SELECT id,actor_id,payload FROM events WHERE project_id=:p "
                            "AND event_type='mail_plugin_action' "
                            "ORDER BY created_at"
                        ),
                        {"p": project_id},
                    )
                    .mappings()
                    .all()
                )
                assert len(receipts) == 10
                assert all(receipt["actor_id"] == configured.actor_id for receipt in receipts)
                content = json.dumps([receipt["payload"] for receipt in receipts])
                for excluded in [
                    fixture.password,
                    fixture.username,
                    "buyer@",
                    "Synthetic mail body",
                    "Synthetic pricing",
                    identifier,
                    "synthetic-rejected-secret",
                ]:
                    assert excluded not in content
            admin = create_engine(Settings().database_url, hide_parameters=True)
            try:
                for statement in [
                    "UPDATE events SET event_type='changed' WHERE id=:id",
                    "DELETE FROM events WHERE id=:id",
                ]:
                    with pytest.raises(DBAPIError, match="events are append-only"):
                        with admin.begin() as connection:
                            connection.execute(text(statement), {"id": receipts[0]["id"]})
            finally:
                admin.dispose()
    finally:
        engine.dispose()
