"""Signed webhook receipt, deduplication and automation on disposable PostgreSQL."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from onefoundry import coordination_store
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}
SECRET = "synthetic-webhook-signing-secret-0123456789"
ROTATED_SECRET = "synthetic-rotated-signing-secret-abcdefghij"


def _signed(
    delivery: str, body: bytes, *, timestamp: int | None = None, secret: str = SECRET
) -> dict[str, str]:
    seconds = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(
        secret.encode(),
        seconds.encode() + b"." + delivery.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-OneFoundry-Timestamp": seconds,
        "X-OneFoundry-Delivery": delivery,
        "X-OneFoundry-Signature": "sha256=" + digest,
    }


def test_signed_webhook_dedup_project_scope_and_rule_draft(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
        coordination_mode="draft_only",
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": f"Webhook fixture {uuid4()}"}, headers=ORIGIN)
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        other = client.post(
            "/api/projects", json={"name": f"Other webhook fixture {uuid4()}"}, headers=ORIGIN
        )
        assert other.status_code == 201, other.text
        base = f"/api/projects/{project_id}"
        created = client.post(
            f"{base}/extensions/webhooks",
            json={"name": "Synthetic incoming", "signing_secret": SECRET},
            headers=ORIGIN,
        )
        assert created.status_code == 201, created.text
        webhook = created.json()
        assert webhook["enabled"] is False and webhook["credential_present"] is True
        assert "secret" not in json.dumps(webhook).lower()
        assert client.get(f"/api/projects/{other.json()['id']}/extensions/webhooks").json() == []
        url = f"/api/extensions/webhooks/{webhook['id']}"
        body = json.dumps({"event_type": "lead.created", "subject": "Example lead"}).encode()
        headers = _signed("delivery-1", body)
        assert client.post(url, content=body, headers=headers).status_code == 404
        changed = client.patch(
            f"{base}/extensions/webhooks/{webhook['id']}",
            json={"expected_version": webhook["version"], "enabled": True},
            headers=ORIGIN,
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["version"] == webhook["version"] + 1
        stale = client.patch(
            f"{base}/extensions/webhooks/{webhook['id']}",
            json={"expected_version": webhook["version"], "enabled": False},
            headers=ORIGIN,
        )
        assert stale.status_code == 409

        profiles = {row["name"]: row["id"] for row in client.get(f"{base}/profiles").json()}
        rule = client.post(
            f"{base}/coordination/rules",
            json={
                "name": "Webhook lead to Sales",
                "signal_type": "webhook.received",
                "filters": {"event_type": "lead.created"},
                "target_profile_id": profiles["Sales"],
                "title": "Review synthetic lead",
                "instructions": "Draft a response for review.",
            },
            headers=ORIGIN,
        )
        assert rule.status_code == 201, rule.text
        assert (
            client.post(url, content=body, headers={**headers, "origin": "http://evil.test"}).status_code
            == 403
        )
        first = client.post(url, content=body, headers=headers)
        assert first.status_code == 202, first.text
        assert first.json()["duplicate"] is False
        oversized = b"{" + b'"padding":"' + b"x" * (settings.extension_webhook_max_bytes + 1) + b'"}'
        assert client.post(url, content=oversized, headers=_signed("oversized", oversized)).status_code == 413
        repeated = client.post(url, content=body, headers=headers)
        assert repeated.status_code == 202 and repeated.json()["duplicate"] is True
        changed_body = b'{"event_type":"lead.created","subject":"Different"}'
        assert (
            client.post(url, content=changed_body, headers=_signed("delivery-1", changed_body)).status_code
            == 409
        )
        invalid = client.post(
            url, content=body, headers={**headers, "X-OneFoundry-Signature": "sha256=" + "0" * 64}
        )
        assert invalid.status_code == 401
        assert (
            client.post(url, content=body, headers=_signed("delivery-2", body, timestamp=1)).status_code
            == 401
        )
        assert (
            client.post(url, content=b"not json", headers=_signed("delivery-3", b"not json")).status_code
            == 400
        )
        signals = client.get(f"{base}/coordination/signals").json()
        assert len([signal for signal in signals if signal["signal_type"] == "webhook.received"]) == 1
        engine = create_engine(settings.database_url)
        try:
            with engine.connect() as connection:
                count = connection.scalar(
                    text(
                        "SELECT count(*) FROM extension_webhook_deliveries "
                        "WHERE project_id=:p AND webhook_id=:id"
                    ),
                    {"p": UUID(project_id), "id": UUID(webhook["id"])},
                )
            assert count == 1
            coordinator = ToolGateway(
                engine, settings.onefoundry_coordinator_actor_id, live_text_settings=settings
            )
            for _ in range(8):
                if not coordination_store.run_once(coordinator, settings, UUID(project_id)):
                    break
            orders = client.get(f"{base}/work-orders").json()
            assert len([order for order in orders if order["title"] == "Review synthetic lead"]) == 1
        finally:
            engine.dispose()
        rotate = client.put(
            f"{base}/extensions/webhooks/{webhook['id']}/credential",
            json={"expected_version": changed.json()["version"], "value": ROTATED_SECRET},
            headers=ORIGIN,
        )
        assert rotate.status_code == 200, rotate.text
        assert rotate.json()["version"] == changed.json()["version"] + 1
        assert ROTATED_SECRET not in rotate.text
        assert client.post(url, content=body, headers=_signed("delivery-4", body)).status_code == 401
        assert (
            client.post(
                url,
                content=body,
                headers=_signed("delivery-4", body, secret=ROTATED_SECRET),
            ).status_code
            == 202
        )


def _receipt_counts(engine: Engine, project_id: str, webhook_id: str) -> tuple[int, int, int, int]:
    with engine.connect() as connection:
        parameters = {"p": UUID(project_id), "actor": f"webhook:{webhook_id}"}
        return (
            int(
                connection.scalar(
                    text("SELECT count(*) FROM coordination_signals WHERE project_id=:p"), parameters
                )
            ),
            int(
                connection.scalar(
                    text("SELECT count(*) FROM extension_webhook_deliveries WHERE project_id=:p"), parameters
                )
            ),
            int(
                connection.scalar(
                    text("SELECT count(*) FROM events WHERE project_id=:p AND actor_id=:actor"), parameters
                )
            ),
            int(connection.scalar(text("SELECT count(*) FROM work_orders WHERE project_id=:p"), parameters)),
        )


@pytest.mark.parametrize("interruption", ["coordination_signal_recorded", "extension_webhook_received"])
def test_webhook_transaction_interruption_and_restart_deduplication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    """Interrupt after signal creation or immediately before receipt commit, then retry."""
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
        coordination_mode="draft_only",
    )
    engine = create_engine(settings.database_url)
    interrupted: list[str] = []
    original_event = ToolGateway._event

    def interrupt_after_event(
        gateway: ToolGateway,
        connection: Any,
        project_id: UUID,
        work_order_id: UUID | None,
        run_id: UUID | None,
        event_type: str,
        payload: dict[str, Any],
        actor_id: str | None = None,
    ) -> None:
        original_event(gateway, connection, project_id, work_order_id, run_id, event_type, payload, actor_id)
        if event_type == interruption:
            # Observe the real uncommitted rows before simulating the interrupted request.
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM coordination_signals WHERE project_id=:p"), {"p": project_id}
                )
                == 1
            )
            expected_deliveries = int(event_type == "extension_webhook_received")
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM extension_webhook_deliveries WHERE project_id=:p"),
                    {"p": project_id},
                )
                == expected_deliveries
            )
            interrupted.append(event_type)
            raise RuntimeError("synthetic receipt interruption")

    try:
        with TestClient(
            create_app(settings), base_url="http://localhost:8765", raise_server_exceptions=False
        ) as client:
            project = client.post(
                "/api/projects", json={"name": f"Webhook interrupted {uuid4()}"}, headers=ORIGIN
            )
            assert project.status_code == 201, project.text
            project_id = project.json()["id"]
            base = f"/api/projects/{project_id}"
            created = client.post(
                f"{base}/extensions/webhooks",
                json={"name": "Interrupted incoming", "signing_secret": SECRET},
                headers=ORIGIN,
            )
            assert created.status_code == 201, created.text
            webhook = created.json()
            enabled = client.patch(
                f"{base}/extensions/webhooks/{webhook['id']}",
                json={"expected_version": webhook["version"], "enabled": True},
                headers=ORIGIN,
            )
            assert enabled.status_code == 200, enabled.text
            profiles = {row["name"]: row["id"] for row in client.get(f"{base}/profiles").json()}
            rule = client.post(
                f"{base}/coordination/rules",
                json={
                    "name": "Interrupted receipt to Sales",
                    "signal_type": "webhook.received",
                    "filters": {"event_type": "lead.created"},
                    "target_profile_id": profiles["Sales"],
                    "title": "Review retried synthetic lead",
                    "instructions": "Draft a response for review.",
                },
                headers=ORIGIN,
            )
            assert rule.status_code == 201, rule.text
            url = f"/api/extensions/webhooks/{webhook['id']}"
            body = b'{"event_type":"lead.created","subject":"Interrupted synthetic lead"}'
            headers = _signed("crash-window-delivery", body)
            assert _receipt_counts(engine, project_id, webhook["id"]) == (0, 0, 0, 0)
            with monkeypatch.context() as fault:
                fault.setattr(ToolGateway, "_event", interrupt_after_event)
                failed = client.post(url, content=body, headers=headers)
            assert failed.status_code == 500
            assert interrupted == [interruption]
            # Neither the signal/event nor delivery can escape the owning transaction.
            assert _receipt_counts(engine, project_id, webhook["id"]) == (0, 0, 0, 0)
            accepted = client.post(url, content=body, headers=headers)
            assert accepted.status_code == 202, accepted.text
            assert accepted.json()["duplicate"] is False
            signal_id = accepted.json()["signal_id"]
            assert _receipt_counts(engine, project_id, webhook["id"]) == (1, 1, 2, 0)

        # A fresh app, engine and client must use durable deduplication, not process memory.
        with TestClient(create_app(settings), base_url="http://localhost:8765") as restarted:
            duplicate = restarted.post(url, content=body, headers=headers)
            assert duplicate.status_code == 202, duplicate.text
            assert duplicate.json() == {"status": "accepted", "duplicate": True, "signal_id": signal_id}
            coordinator = ToolGateway(
                engine, settings.onefoundry_coordinator_actor_id, live_text_settings=settings
            )
            for _ in range(8):
                if not coordination_store.run_once(coordinator, settings, UUID(project_id)):
                    break
            counts = _receipt_counts(engine, project_id, webhook["id"])
            assert counts[:3] == (1, 1, 2)
            assert counts[3] == 1
            orders = restarted.get(f"{base}/work-orders").json()
            assert len(orders) == 1 and orders[0]["title"] == "Review retried synthetic lead"
            assert orders[0]["status"] == "draft"
    finally:
        engine.dispose()
