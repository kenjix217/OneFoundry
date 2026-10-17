"""Database-backed Sales draft and exact local approval tests."""

import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from onefoundry.main import create_app
from onefoundry.settings import Settings


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as instance:
        yield instance


def post(client: TestClient, path: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(path, json=body, headers={"origin": "http://localhost:8765"})


def prepared(client: TestClient, title: str = "Sales preview") -> tuple[str, str, str]:
    project_id = post(client, "/api/projects", {"name": "Sales local test"}).json()["id"]
    profiles = {p["name"]: p for p in client.get(f"/api/projects/{project_id}/profiles").json()}
    root = f"/api/projects/{project_id}"
    order = post(
        client,
        f"{root}/work-orders",
        {
            "title": title,
            "instructions": "Use only synthetic fixture text and local preview.",
            "profile_id": profiles["Researcher"]["id"],
        },
    ).json()
    assert post(client, f"{root}/work-orders/{order['id']}/ready").status_code == 200
    run_response = post(
        client,
        f"{root}/work-orders/{order['id']}/synthetic-text-run",
        {
            "fixture": "research_to_gtm",
        },
    )
    assert run_response.status_code == 201, run_response.text
    return project_id, run_response.json()["run"]["id"], profiles["Sales"]["id"]


def test_sales_draft_same_run_exact_approval_and_mailpit_readback(client: TestClient) -> None:
    project_id, run_id, sales_id = prepared(client, "Sales\npreview")
    root = f"/api/projects/{project_id}"
    configured = client.patch(
        f"{root}/profiles/{sales_id}",
        json={"model": "sales-before", "instructions": "Sales draft before"},
        headers={"origin": "http://localhost:8765"},
    )
    assert configured.status_code == 200, configured.text
    response = post(client, f"{root}/runs/{run_id}/sales-draft")
    assert response.status_code == 201, response.text
    changed = client.patch(
        f"{root}/profiles/{sales_id}",
        json={"model": "sales-after", "instructions": "Sales draft after"},
        headers={"origin": "http://localhost:8765"},
    )
    assert changed.status_code == 200, changed.text
    draft = response.json()
    assert draft["source_run_id"] == draft["run_id"] == run_id
    assert draft["recipient"] == "sales-preview@onefoundry.invalid"
    assert "\n" not in draft["subject"]
    assert draft["delivery_status"] == "pending"
    assert draft["actor_id"] == f"profile:{sales_id}"
    assert post(client, f"{root}/runs/{run_id}/sales-draft").status_code == 409
    run = client.get(f"{root}/runs/{run_id}").json()
    assert run["mode"] == "synthetic_text" and run["status"] == "done"
    assert run["profile_id"] == sales_id
    assert [item["profile_name"] for item in run["profile_snapshots"]] == ["Researcher", "GTM", "Sales"]
    assert run["profile_snapshots"][-1]["profile_model"] == "sales-before"
    assert run["profile_snapshots"][-1]["profile_instructions"] == "Sales draft before"
    assert "synthetic_sales_handed_off" in [e["event_type"] for e in run["events"]]
    assert [o["role"] for o in client.get(f"{root}/runs/{run_id}/text-outputs").json()] == [
        "Researcher",
        "GTM",
    ]
    pending = client.get(f"{root}/approvals?status=pending").json()
    approval = next(a for a in pending if a["id"] == draft["approval_id"])
    assert approval["synthetic_sales"] is True
    assert approval["target"] == draft["recipient"]
    assert approval["content"] == f"Subject: {draft['subject']}\n\n{draft['body']}"
    with (
        patch("onefoundry.sales_store.smtplib.SMTP") as smtp,
        patch("onefoundry.sales_store.SalesStore._mailpit_readback", return_value=True) as readback,
    ):
        approved = post(
            client,
            f"{root}/approvals/{draft['approval_id']}/decision",
            {
                "decision": "approved",
            },
        )
    assert approved.status_code == 200, approved.text
    smtp.assert_called_once()
    sent_message = smtp.return_value.__enter__.return_value.send_message.call_args
    assert sent_message.kwargs["to_addrs"] == ["sales-preview@onefoundry.invalid"]
    readback.assert_called_once()
    saved = client.get(f"{root}/runs/{run_id}/sales-draft").json()
    assert saved["delivery_status"] == "delivered" and saved["sent_at"] is not None
    assert (
        post(
            client,
            f"{root}/approvals/{draft['approval_id']}/decision",
            {
                "decision": "approved",
            },
        ).status_code
        == 409
    )


def test_rejection_never_sends_and_unknown_never_retries(client: TestClient) -> None:
    for decision, expected in (("rejected", "rejected"), ("approved", "unknown")):
        project_id, run_id, _ = prepared(client)
        root = f"/api/projects/{project_id}"
        draft = post(client, f"{root}/runs/{run_id}/sales-draft").json()
        with (
            patch("onefoundry.sales_store.smtplib.SMTP") as smtp,
            patch("onefoundry.sales_store.SalesStore._mailpit_readback", return_value=False),
        ):
            response = post(
                client,
                f"{root}/approvals/{draft['approval_id']}/decision",
                {
                    "decision": decision,
                },
            )
        assert response.status_code == 200, response.text
        assert smtp.call_count == (0 if decision == "rejected" else 1)
        assert client.get(f"{root}/runs/{run_id}/sales-draft").json()["delivery_status"] == expected
        with patch("onefoundry.sales_store.smtplib.SMTP") as repeated:
            assert (
                post(
                    client,
                    f"{root}/approvals/{draft['approval_id']}/decision",
                    {
                        "decision": "approved",
                    },
                ).status_code
                == 409
            )
        repeated.assert_not_called()
