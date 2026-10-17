"""Real database isolation, secret ownership and one-shot approval dispatch."""

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from onefoundry import role_tool_admission
from onefoundry.gateway_errors import Conflict
from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_schemas import ApprovalDecision
from onefoundry.zoho_mail_contracts import ZohoCredentials, ZohoSetup, ZohoWrite
from onefoundry.zoho_mail_dispatch import dispatch_once, reconcile_interrupted
from onefoundry.zoho_mail_mcp import ZohoMcpError
from onefoundry.zoho_mail_store import ZohoMailStore
from tests.test_role_tool_admission_postgres import _run
from tests.test_smtp_postgres import _manual_run, _post

ORIGIN = {"origin": "http://localhost:8765"}


class Provider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    def call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, arguments))
        if operation == "getMailAccounts":
            value: Any = [{"accountId": "12345", "primaryEmailAddress": "owner@example.test"}]
        elif operation in {"sendEmail", "sendReplyEmail"}:
            if self.fail:
                raise ZohoMcpError("connection_or_authorization_failed")
            value = {"messageId": "67890"}
        else:
            value = []
        return {"status": "success", "data": {"status": {"code": 200}, "data": value}}


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )


def _connect(store: ZohoMailStore, project_id: UUID) -> dict[str, Any]:
    row = store.create(project_id, ZohoSetup(label="Zoho Mail"))
    assert row["status"] == "needs_credentials" and not row["credential_present"] and not row["enabled"]
    row = store.credentials(
        project_id,
        row["id"],
        ZohoCredentials(
            expected_version=row["version"],
            server_url=SecretStr(f"https://mail-fixture.zohomcp.com/mcp/{uuid4().hex}/message"),
            access_token=SecretStr("synthetic-access"),
        ),
    )
    row = store.probe(project_id, row["id"])
    assert row["status"] == "ready" and not row["enabled"]
    return store.set_enabled(project_id, row["id"], True, row["version"])


def test_setup_without_credentials_is_private_project_bound_and_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = _post(client, "/api/projects", {"name": f"Zoho setup {uuid4()}"})
        other = _post(client, "/api/projects", {"name": f"Zoho other {uuid4()}"})
        base = f"/api/projects/{project['id']}/mail/zoho/connections"
        row = _post(client, base, {"label": "Zoho Mail"})
        assert row["status"] == "needs_credentials" and row["enabled"] is False
        assert row["credential_present"] is False and row["last_probe_at"] is None
        assert (
            client.post(
                f"{base}/{row['id']}/enabled",
                headers=ORIGIN,
                json={"enabled": True, "expected_version": row["version"]},
            ).status_code
            == 503
        )
        assert client.post(f"{base}/{row['id']}/probe", headers=ORIGIN).status_code == 503
        other_base = f"/api/projects/{other['id']}/mail/zoho/connections"
        assert client.get(other_base).json() == []
        assert client.post(f"{other_base}/{row['id']}/probe", headers=ORIGIN).status_code == 404
        assert (
            client.post(base, json={"label": "Other"}, headers={"origin": "https://foreign.test"}).status_code
            == 403
        )


@pytest.mark.parametrize("operation,uncertain", [("draft", False), ("send", False), ("send", True)])
def test_exact_approval_dispatch_and_unknown_are_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    uncertain: bool,
) -> None:
    settings, provider = _settings(tmp_path), Provider()
    monkeypatch.setattr(ZohoMailStore, "client", lambda self, row: provider)
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    store = gateway._zoho_mail()
    store.prepare_secrets()
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = _post(client, "/api/projects", {"name": f"Zoho approval {uuid4()}"})
            project_id = UUID(project["id"])
            base = f"/api/projects/{project_id}"
            run_id = _manual_run(client, base)
            row = _connect(store, project_id)
            vault = store.vault(row)
            info = (store.secrets.root / str(row["id"])).stat()
            assert stat.S_IMODE(info.st_mode) == 0o600
            fields = {
                "connection_id": str(row["id"]),
                "operation": operation,
                "run_id": run_id,
                "to_address": "buyer@example.test",
                "subject": "Hello",
                "body": "Exact reviewed text.",
            }
            proposed = _post(client, f"{base}/mail/zoho/actions", fields)
            action_id, approval_id = UUID(proposed["action_id"]), UUID(proposed["approval_id"])
            approvals = client.get(f"{base}/approvals?status=pending").json()
            assert (
                next(item for item in approvals if item["id"] == str(approval_id))["zoho_mail_operation"]
                == operation
            )
            assert len(provider.calls) == 1  # Only the read-only account probe.
            with pytest.raises(Conflict):
                dispatch_once(store, project_id, action_id)
            assert len(provider.calls) == 1
            provider.fail = uncertain
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        gateway.decide_approval,
                        project_id,
                        approval_id,
                        ApprovalDecision(decision="approved"),
                    )
                    for _ in range(2)
                ]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except Conflict:
                        pass
            assert len(outcomes) == 1
            assert outcomes[0]["zoho_mail_operation"] == operation
            assert len([call for call in provider.calls if call[0] == "sendEmail"]) == 1
            sent = provider.calls[-1][1]
            assert sent["body"].get("mode") == ("draft" if operation == "draft" else None)
            assert sent["body"]["content"] == fields["body"]
            with engine.connect() as connection:
                saved = (
                    connection.execute(
                        text("SELECT state,proposal FROM zoho_mail_actions WHERE id=:id"), {"id": action_id}
                    )
                    .mappings()
                    .one()
                )
                events = str(
                    connection.execute(
                        text("SELECT event_type,payload FROM events WHERE project_id=:p"), {"p": project_id}
                    ).all()
                )
            assert saved["state"] == ("unknown" if uncertain else "accepted")
            assert vault["server_url"] not in events and vault["access_token"] not in events
            assert vault["server_url"] not in json.dumps(saved["proposal"])
            with pytest.raises(Conflict):
                dispatch_once(store, project_id, action_id)
            assert len(provider.calls) == 2
            # A process crash after the durable claim never queues another send.
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE zoho_mail_actions SET state='dispatching' WHERE id=:id"), {"id": action_id}
                )
            reconcile_interrupted(store)
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT state FROM zoho_mail_actions WHERE id=:id"), {"id": action_id}
                    )
                    == "unknown"
                )
            with pytest.raises(Conflict):
                dispatch_once(store, project_id, action_id)
            assert len(provider.calls) == 2
            rejected = _post(client, f"{base}/mail/zoho/actions", fields)
            gateway.decide_approval(
                project_id, UUID(rejected["approval_id"]), ApprovalDecision(decision="rejected")
            )
            assert len(provider.calls) == 2
    finally:
        engine.dispose()


def test_connection_changes_invalidate_pending_proposals_and_role_access_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, provider = _settings(tmp_path), Provider()
    monkeypatch.setattr(ZohoMailStore, "client", lambda self, row: provider)
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    store = gateway._zoho_mail()
    store.prepare_secrets()
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            pid, rid, profile_id = _run(client, gateway, tools=["zoho_mail_read_v1", "propose_zoho_mail_v1"])
            row = _connect(store, pid)
            discovered = role_tool_admission.invoke_tool(
                gateway,
                pid,
                rid,
                profile_id,
                "discover-zoho",
                "zoho_mail_read_v1",
                {"operation": "connections"},
                tool_version=1,
            )
            assert str(discovered["result"]["connections"][0]["id"]) == str(row["id"])
            assert len(provider.calls) == 1
            result = role_tool_admission.invoke_tool(
                gateway,
                pid,
                rid,
                profile_id,
                "zoho-read",
                "zoho_mail_read_v1",
                {"connection_id": str(row["id"]), "operation": "folders"},
                tool_version=1,
            )
            assert result["status"] == "done"
            fields = ZohoWrite(
                connection_id=row["id"],
                operation="draft",
                to_address="buyer@example.test",
                subject="Hello",
                body="Draft",
            )
            proposed = role_tool_admission.invoke_tool(
                gateway,
                pid,
                rid,
                profile_id,
                "zoho-propose",
                "propose_zoho_mail_v1",
                fields.model_dump(mode="json"),
                tool_version=1,
            )
            assert proposed["result"]["state"] == "pending"
            store.set_enabled(pid, row["id"], False, row["version"])
            with pytest.raises(Conflict):
                gateway.decide_approval(
                    pid, UUID(proposed["result"]["approval_id"]), ApprovalDecision(decision="approved")
                )
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE profiles SET allowed_tools='[]'::jsonb WHERE id=:id"), {"id": profile_id}
                )
            with pytest.raises(ToolDenied):
                role_tool_admission.invoke_tool(
                    gateway,
                    pid,
                    rid,
                    profile_id,
                    "revoked",
                    "zoho_mail_read_v1",
                    {"connection_id": str(row["id"]), "operation": "folders"},
                    tool_version=1,
                )
            assert all(call[0] not in {"sendEmail", "sendReplyEmail"} for call in provider.calls)
    finally:
        engine.dispose()
