"""Approval-bound SMTP setup and one-shot submission against PostgreSQL."""

import os
import smtplib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry import smtp_dispatch
from onefoundry.gateway_errors import Conflict
from onefoundry.mail_imap import parse_message
from onefoundry.mail_lineage import reply_lineage
from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.smtp_accounts import SmtpAccounts
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_schemas import ApprovalDecision
from tests.test_role_tool_admission_postgres import _run

ORIGIN = {"origin": "http://localhost:8765"}


def _post(client: TestClient, path: str, data: dict[str, Any] | None = None) -> Any:
    response = client.post(path, json=data or {}, headers=ORIGIN)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def _manual_run(client: TestClient, base: str) -> str:
    profile_id = client.get(f"{base}/profiles").json()[0]["id"]
    order = _post(
        client,
        f"{base}/work-orders",
        {
            "title": "Prepare a customer reply",
            "instructions": "Draft an exact email.",
            "profile_id": profile_id,
        },
    )
    _post(client, f"{base}/work-orders/{order['id']}/ready")
    return str(_post(client, f"{base}/work-orders/{order['id']}/runs")["id"])


def test_outbound_mail_requires_exact_approval_and_never_retries_unknown(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    submitted: list[tuple[str, str, str]] = []
    real_dispatch = smtp_dispatch.dispatch_once

    def accepted(gateway: ToolGateway, accounts: SmtpAccounts, project_id: UUID, email_id: UUID) -> None:
        real_dispatch(
            gateway,
            accounts,
            project_id,
            email_id,
            sender=lambda _config, sender, recipient, subject, _body, _message_id: submitted.append(
                (sender, recipient, subject)
            ),
        )

    monkeypatch.setattr(smtp_dispatch, "dispatch_once", accepted)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = _post(client, "/api/projects", {"name": f"Outgoing {uuid4()}"})
            base = f"/api/projects/{project['id']}"
            run_id = _manual_run(client, base)
            outgoing = f"{base}/mail/outgoing"
            invalid = client.post(
                f"{outgoing}/accounts",
                headers=ORIGIN,
                json={
                    "label": "Invalid",
                    "host": "smtp://example.test",
                    "username": "owner@example.test",
                    "from_address": "owner@example.test",
                    "password": "fixture password",
                },
            )
            assert invalid.status_code == 422
            account = _post(
                client,
                f"{outgoing}/accounts",
                {
                    "label": "Company",
                    "host": "smtp.example.test",
                    "port": 465,
                    "security": "ssl",
                    "username": "owner@example.test",
                    "from_address": "owner@example.test",
                    "password": "fixture password",
                },
            )
            assert account["credential_present"] and "password" not in account
            assert account["enabled"] is False
            assert (
                client.post(
                    f"{outgoing}/accounts/{account['id']}/enabled",
                    headers=ORIGIN,
                    json={"enabled": True, "expected_version": account["version"]},
                ).status_code
                == 409
            )

            def bad_login(_config: Any) -> None:
                raise smtplib.SMTPAuthenticationError(535, b"fixture denied")

            failed = SmtpAccounts(gateway, settings, check=bad_login).probe(
                UUID(project["id"]), UUID(account["id"])
            )
            assert failed["status"] == "failed" and failed["last_error_code"] == "authentication_failed"
            assert failed["enabled"] is False
            direct = SmtpAccounts(gateway, settings, check=lambda _config: None)
            checked = direct.probe(UUID(project["id"]), UUID(account["id"]))
            assert checked["status"] == "ready"
            enabled = _post(
                client,
                f"{outgoing}/accounts/{account['id']}/enabled",
                {"enabled": True, "expected_version": checked["version"]},
            )
            assert enabled["enabled"] is True
            fields = {
                "account_id": account["id"],
                "run_id": run_id,
                "to_address": "buyer@example.test",
                "subject": "Proposal",
                "body": "Hello buyer.",
            }
            for bad_fields in (
                {"to_address": "buyer@example.test,spy@example.test"},
                {"subject": "Hello\nBcc: spy@example.test"},
                {"body": "password=fixture"},
            ):
                assert (
                    client.post(f"{outgoing}/emails", headers=ORIGIN, json=fields | bad_fields).status_code
                    == 422
                )
            proposed = _post(client, f"{outgoing}/emails", fields)
            assert submitted == []
            pending = client.get(f"{base}/approvals?status=pending").json()
            approval = next(row for row in pending if row["id"] == proposed["approval_id"])
            assert approval["outbound_email"] is True
            assert (
                approval["content"]
                == "From: owner@example.test\nTo: buyer@example.test\nSubject: Proposal\n\nHello buyer."
            )
            _post(client, f"{base}/approvals/{approval['id']}/decision", {"decision": "rejected"})
            assert submitted == []
            assert client.get(f"{outgoing}/emails").json()[0]["state"] == "rejected"

            proposed = _post(client, f"{outgoing}/emails", fields)
            decided = _post(
                client, f"{base}/approvals/{proposed['approval_id']}/decision", {"decision": "approved"}
            )
            assert decided["outbound_email"] is True
            assert submitted == [("owner@example.test", "buyer@example.test", "Proposal")]
            saved = client.get(f"{outgoing}/emails").json()[0]
            assert saved["state"] == "accepted" and saved["attempted_at"] is not None
            owner = create_engine(
                Settings(db_user="onefoundry", db_password=os.environ["DB_PASSWORD"]).database_url
            )
            with owner.begin() as connection:
                connection.execute(
                    text("UPDATE work_orders SET chain_depth=10 WHERE project_id=:p AND id=:o"),
                    {"p": UUID(project["id"]), "o": UUID(saved["work_order_id"])},
                )
            owner.dispose()
            with engine.connect() as connection:
                header = f"References: <unrelated@example.test> <{saved['id']}@onefoundry.local>\r\n\r\nReply"
                reply = parse_message(header.encode())
                inherited = reply_lineage(connection, UUID(project["id"]), reply)
                assert inherited == {
                    "origin_work_order_id": UUID(saved["work_order_id"]),
                    "origin_run_id": UUID(run_id),
                    "chain_depth": 10,
                }
                assert reply_lineage(connection, uuid4(), reply) == {}
                assert reply_lineage(connection, UUID(project["id"]), parse_message(b"\r\nNew")) == {}
            timeline = client.get(f"{base}/runs/{run_id}").json()["events"]
            assert any(event["event_type"] == "outbound_email_accepted" for event in timeline)
            assert (
                client.post(
                    f"{base}/approvals/{proposed['approval_id']}/decision",
                    headers=ORIGIN,
                    json={"decision": "approved"},
                ).status_code
                == 409
            )

            def unknown(
                gateway: ToolGateway, accounts: SmtpAccounts, project_id: UUID, email_id: UUID
            ) -> None:
                def timeout(*_args: Any) -> None:
                    raise TimeoutError("fixture uncertainty")

                real_dispatch(gateway, accounts, project_id, email_id, sender=timeout)

            monkeypatch.setattr(smtp_dispatch, "dispatch_once", unknown)
            proposed = _post(client, f"{outgoing}/emails", fields | {"subject": "Unknown"})
            _post(client, f"{base}/approvals/{proposed['approval_id']}/decision", {"decision": "approved"})
            assert client.get(f"{outgoing}/emails").json()[0]["state"] == "unknown"
            timeline = client.get(f"{base}/runs/{run_id}").json()["events"]
            assert any(event["event_type"] == "outbound_email_unknown" for event in timeline)
            assert (
                client.post(
                    f"{base}/approvals/{proposed['approval_id']}/decision",
                    headers=ORIGIN,
                    json={"decision": "approved"},
                ).status_code
                == 409
            )
            assert len(submitted) == 1
            monkeypatch.setattr(smtp_dispatch, "dispatch_once", accepted)
            competing = _post(client, f"{outgoing}/emails", fields | {"subject": "One claim"})
            barrier = Barrier(2)

            def decide_concurrently() -> bool:
                barrier.wait()
                try:
                    gateway.decide_approval(
                        UUID(project["id"]),
                        UUID(competing["approval_id"]),
                        ApprovalDecision(decision="approved"),
                    )
                except Conflict:
                    return False
                return True

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: decide_concurrently(), range(2)))
            assert results.count(True) == 1 and results.count(False) == 1
            assert submitted.count(("owner@example.test", "buyer@example.test", "One claim")) == 1
            second = _post(client, "/api/projects", {"name": f"Other {uuid4()}"})
            assert client.get(f"/api/projects/{second['id']}/mail/outgoing/emails").json() == []
    finally:
        engine.dispose()


def test_agent_tool_only_proposes_email_with_current_and_snapshot_permission(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project_id, run_id, profile_id = _run(client, gateway, tools=["propose_email_v1"])
            base = f"/api/projects/{project_id}"
            account = _post(
                client,
                f"{base}/mail/outgoing/accounts",
                {
                    "label": "Agent sender",
                    "host": "smtp.example.test",
                    "port": 465,
                    "security": "ssl",
                    "username": "owner@example.test",
                    "from_address": "owner@example.test",
                    "password": "fixture password",
                },
            )
            checked = SmtpAccounts(gateway, settings, check=lambda _config: None).probe(
                project_id, UUID(account["id"])
            )
            _post(
                client,
                f"{base}/mail/outgoing/accounts/{account['id']}/enabled",
                {"enabled": True, "expected_version": checked["version"]},
            )
            assert "propose_email_v1" in {
                row["id"] for row in gateway.available_role_tools(project_id, run_id, profile_id)
            }
            receipt = gateway.invoke_role_tool(
                project_id,
                run_id,
                profile_id,
                "email-one",
                "propose_email_v1",
                {
                    "account_id": account["id"],
                    "to_address": "buyer@example.test",
                    "subject": "A proposal",
                    "body": "Please review this note.",
                },
                tool_version=1,
            )
            assert receipt["status"] == "done"
            saved = client.get(f"{base}/mail/outgoing/emails").json()
            assert (
                saved[0]["state"] == "pending" and saved[0]["approval_id"] == receipt["result"]["approval_id"]
            )
            denied = client.patch(f"{base}/profiles/{profile_id}", headers=ORIGIN, json={"allowed_tools": []})
            assert denied.status_code == 200, denied.text
            try:
                gateway.invoke_role_tool(
                    project_id,
                    run_id,
                    profile_id,
                    "email-two",
                    "propose_email_v1",
                    {
                        "account_id": account["id"],
                        "to_address": "buyer@example.test",
                        "subject": "No send",
                        "body": "This must be denied.",
                    },
                    tool_version=1,
                )
            except ToolDenied:
                pass
            else:
                raise AssertionError("revoked agent email tool was admitted")
            assert len(client.get(f"{base}/mail/outgoing/emails").json()) == 1
    finally:
        engine.dispose()
