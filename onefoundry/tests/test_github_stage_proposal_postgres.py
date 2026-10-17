"""GitHub staging proposal binds inbox decision to a saved source, without fetch."""

import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}
PIN = "a" * 40
TOKEN = "fixture-credential-never-used-for-staging-proposal"


def post(client: TestClient, path: str, payload: dict[str, object] | None = None) -> Response:
    return client.post(path, json=payload, headers=ORIGIN)


def test_exact_proposal_inbox_decision_and_stale_readback(tmp_path: Path) -> None:
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = post(client, "/api/projects", {"name": "Stage proposal"}).json()
        other = post(client, "/api/projects", {"name": "Other project"}).json()
        path = f"/api/projects/{project['id']}"
        profile = next(item for item in client.get(f"{path}/profiles").json() if item["name"] == "Engineer")
        order = post(
            client,
            f"{path}/work-orders",
            {
                "title": "Review staging",
                "instructions": "Propose exact repository stage",
                "profile_id": profile["id"],
            },
        ).json()
        assert post(client, f"{path}/work-orders/{order['id']}/ready").status_code == 200
        run = post(client, f"{path}/work-orders/{order['id']}/runs").json()
        account = post(
            client,
            f"{path}/github/accounts",
            {
                "label": "Fixture account",
                "token": TOKEN,
            },
        )
        assert account.status_code == 201, account.text
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", PIN)):
            source = post(
                client,
                f"{path}/sources/github",
                {
                    "account_id": account.json()["id"],
                    "owner": "sample",
                    "repo": "project",
                    "branch": "main",
                },
            )
        assert source.status_code == 201, source.text
        lookalike = post(
            client,
            f"{path}/runs/{run['id']}/approval-requests",
            {
                "action_type": "other",
                "target": "sample/project",
                "content": '{"kind":"github_stage_v1","project_id":"' + project["id"] + '"}',
            },
        )
        assert lookalike.status_code == 201, lookalike.text
        endpoint = f"{path}/tools/github/stage-proposals"
        with patch("onefoundry.source_store.resolve_revision") as no_network:
            proposed = post(client, endpoint, {"run_id": run["id"], "source_id": source.json()["id"]})
        no_network.assert_not_called()
        assert proposed.status_code == 201, proposed.text
        result = proposed.json()
        assert result["stage_status"] == "not_launched"
        assert result["source"] == {
            "kind": "github_stage_v1",
            "project_id": project["id"],
            "source_id": source.json()["id"],
            "account_id": account.json()["id"],
            "owner": "sample",
            "repo": "project",
            "branch": "main",
            "saved_revision": PIN,
        }
        approval_id = result["approval"]["id"]
        assert result["approval"]["status"] == "pending"
        assert TOKEN not in proposed.text
        assert (
            post(client, endpoint, {"run_id": run["id"], "source_id": source.json()["id"]}).status_code == 409
        )
        assert client.get(f"{endpoint}/{approval_id}").json()["source_current"] is True
        other_status = client.get(f"/api/projects/{other['id']}/tools/github/stage-proposals/{approval_id}")
        assert other_status.status_code == 404
        assert (
            post(
                client,
                f"/api/projects/{other['id']}/tools/github/stage-proposals",
                {"run_id": run["id"], "source_id": source.json()["id"]},
            ).status_code
            == 404
        )
        assert (
            post(client, endpoint, {"run_id": str(uuid4()), "source_id": source.json()["id"]}).status_code
            == 404
        )
        inbox = client.get(f"{path}/approvals?status=pending").json()
        assert any(
            item["id"] == approval_id and PIN in item["content"] and item["github_stage"] is True
            for item in inbox
        )
        assert next(item for item in inbox if item["id"] == lookalike.json()["id"])["github_stage"] is False
        decision = post(client, f"{path}/approvals/{approval_id}/decision", {"decision": "approved"})
        assert decision.status_code == 200, decision.text
        assert (
            post(client, f"{path}/approvals/{approval_id}/decision", {"decision": "approved"}).status_code
            == 409
        )
        status = client.get(f"{endpoint}/{approval_id}")
        assert status.status_code == 200, status.text
        assert status.json()["decision"] == "approved"
        assert status.json()["stage_status"] == "not_launched"
        assert (
            post(client, endpoint, {"run_id": run["id"], "source_id": source.json()["id"]}).status_code == 409
        )
        assert client.delete(f"{path}/sources/{source.json()['id']}", headers=ORIGIN).status_code == 204
        stale = client.get(f"{endpoint}/{approval_id}")
        assert stale.status_code == 200
        assert stale.json()["source_current"] is False
        assert stale.json()["stage_status"] == "not_launched"
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", PIN)):
            stale_source = post(
                client,
                f"{path}/sources/github",
                {
                    "account_id": account.json()["id"],
                    "owner": "sample",
                    "repo": "project",
                    "branch": "main",
                },
            )
        assert stale_source.status_code == 201, stale_source.text
        stale_proposal = post(
            client,
            endpoint,
            {
                "run_id": run["id"],
                "source_id": stale_source.json()["id"],
            },
        )
        assert stale_proposal.status_code == 201, stale_proposal.text
        stale_approval_id = stale_proposal.json()["approval"]["id"]
        assert client.delete(f"{path}/sources/{stale_source.json()['id']}", headers=ORIGIN).status_code == 204
        rejected_stale = post(
            client, f"{path}/approvals/{stale_approval_id}/decision", {"decision": "approved"}
        )
        assert rejected_stale.status_code == 409
        pending = client.get(f"{path}/approvals?status=pending").json()
        assert next(item for item in pending if item["id"] == stale_approval_id)["github_stage"] is True
        assert not any(
            item["id"] == stale_approval_id for item in client.get(f"{path}/approvals?status=approved").json()
        )
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            events = connection.execute(
                text(
                    "SELECT event_type,payload FROM events WHERE project_id=:project_id "
                    "AND event_type='github_stage_proposed'"
                ),
                {"project_id": project["id"]},
            ).all()
            stale_decision_count = connection.scalar(
                text(
                    "SELECT count(*) FROM events WHERE project_id=:project_id "
                    "AND event_type='approval_decided' AND payload->>'approval_id'=:approval_id"
                ),
                {"project_id": project["id"], "approval_id": stale_approval_id},
            )
        engine.dispose()
        assert len(events) == 2
        assert all(event.payload["saved_revision"] == PIN for event in events)
        assert stale_decision_count == 0
        assert TOKEN not in str(events)
