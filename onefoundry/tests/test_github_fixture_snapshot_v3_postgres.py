"""Approval-bound fake HTTPS snapshots become immutable DB evidence only after bundle readback."""

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.settings import Settings
from runner.github_fixture_snapshot_supervisor import HTTPSFixtureConfig, acquire_supervisor, process_job
from runner.local_source_stage import StageLimits
from tests.test_github_https_fixture_fetch import COMMIT, TOKEN, TREE, fixture

ORIGIN = {"origin": "http://localhost:8765"}


def post(client: TestClient, path: str, payload: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
    return client.post(path, json=payload, headers=ORIGIN)


def test_v3_snapshot_requires_new_approval_and_exact_bundle_readback(tmp_path: Path) -> None:
    spool_root = tmp_path / "spool"
    spool = spool_root / "github-fixture-https-snapshot-v3"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    snapshots = tmp_path / "source-snapshots"
    (snapshots / "snapshots").mkdir(parents=True)
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secrets),
        runner_spool_root=str(spool_root),
        github_snapshot_root=str(snapshots),
        runner_wall_seconds=1,
        runner_receipt_wait_seconds=31,
    )
    with fixture(tmp_path) as (endpoint, cert, handler):
        supervisor = HTTPSFixtureConfig(
            spool, endpoint, cert, TOKEN, StageLimits(10, 4096, 4096, 4), snapshots
        )
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = post(client, "/api/projects", {"name": "Synthetic snapshot v3"}).json()
            base = f"/api/projects/{project['id']}"
            engineer = next(
                item for item in client.get(f"{base}/profiles").json() if item["name"] == "Engineer"
            )
            order = post(
                client,
                f"{base}/work-orders",
                {
                    "title": "Synthetic source snapshot",
                    "instructions": "Test the local fixture",
                    "profile_id": engineer["id"],
                },
            ).json()
            assert post(client, f"{base}/work-orders/{order['id']}/ready").status_code == 200
            run = post(client, f"{base}/work-orders/{order['id']}/runs").json()
            account = post(
                client,
                f"{base}/github/accounts",
                {
                    "label": "Dummy saved account",
                    "token": "saved-token-never-goes-to-fixture",
                },
            ).json()

            def new_source() -> str:
                with patch("onefoundry.source_store.resolve_revision", return_value=("main", COMMIT)):
                    response = post(
                        client,
                        f"{base}/sources/github",
                        {
                            "account_id": account["id"],
                            "owner": "sample",
                            "repo": "project",
                            "branch": "main",
                        },
                    )
                assert response.status_code == 201, response.text
                return str(response.json()["id"])

            endpoint_path = f"{base}/tools/github/fixture-snapshot-v3"
            source = new_source()
            key = str(uuid4())
            proposal_payload = {"run_id": run["id"], "source_id": source, "idempotency_key": key}
            proposed = post(client, endpoint_path, proposal_payload)
            assert proposed.status_code == 201, proposed.text
            approval = proposed.json()["approval"]["id"]
            assert proposed.json()["snapshot_status"] == "not_launched"
            assert proposed.json()["source"]["kind"] == "github_fixture_snapshot_v3"
            assert post(client, endpoint_path, proposal_payload).json()["approval"]["id"] == approval
            assert post(client, f"{endpoint_path}/{approval}/dispatch").status_code == 409
            assert client.get(f"{base}/tools/github/fixture-fetch-v2/{approval}").status_code == 404
            assert (
                post(client, f"{base}/approvals/{approval}/decision", {"decision": "approved"}).status_code
                == 200
            )
            assert post(client, f"{endpoint_path}/{approval}/dispatch").status_code == 503
            descriptor = acquire_supervisor(supervisor)
            try:
                launched = post(client, f"{endpoint_path}/{approval}/dispatch")
                assert launched.status_code == 200, launched.text
                assert launched.json()["snapshot_status"] == "queued"
                attempt = launched.json()["attempt_id"]
                assert post(client, f"{endpoint_path}/{approval}/dispatch").status_code == 409
                manifest = spool / "jobs" / f"{attempt}.json"
                assert manifest.exists()
                assert "saved-token" not in manifest.read_text() and TOKEN not in manifest.read_text()
                receipt = process_job(manifest, supervisor)
                assert receipt is not None and receipt["status"] == "done"
                status = client.get(f"{endpoint_path}/{approval}")
                assert status.status_code == 200, status.text
                assert status.json()["snapshot_status"] == "done"
                assert status.json()["source_tree"] == TREE
                assert status.json()["snapshot_sha256"] == receipt["snapshot_sha256"]
                assert status.json()["snapshot_path"] == receipt["snapshot_path"]
                assert status.json()["snapshot_byte_size"] == receipt["snapshot_byte_size"]
                assert len(handler.observed) == 5
            finally:
                os.close(descriptor)
            with create_engine(settings.database_url).connect() as connection:
                row = (
                    connection.execute(
                        text(
                            "SELECT * FROM github_fixture_source_snapshots WHERE project_id=:project_id "
                            "AND approval_id=:approval_id"
                        ),
                        {"project_id": project["id"], "approval_id": approval},
                    )
                    .mappings()
                    .one()
                )
                events = (
                    connection.execute(
                        text(
                            "SELECT event_type,payload FROM events WHERE project_id=:project_id "
                            "AND payload->>'approval_id'=:approval_id "
                            "AND event_type LIKE 'github_fixture_snapshot_%' ORDER BY created_at,id"
                        ),
                        {"project_id": project["id"], "approval_id": approval},
                    )
                    .mappings()
                    .all()
                )
            assert str(row["attempt_id"]) == attempt
            assert row["source_id"] == UUID(source)
            assert row["account_id"] == UUID(account["id"])
            assert row["source_tree"] == TREE
            assert row["snapshot_path"] == receipt["snapshot_path"]
            assert row["snapshot_sha256"] == receipt["snapshot_sha256"]
            assert (
                row["receipt_sha256"]
                == hashlib.sha256((spool / "receipts" / f"{attempt}.json").read_bytes()).hexdigest()
            )
            assert [event["event_type"] for event in events] == [
                "github_fixture_snapshot_proposed",
                "github_fixture_snapshot_queued",
                "github_fixture_snapshot_receipt",
            ]
            assert "saved-token" not in str(events) and TOKEN not in str(events)

            # A second approval cannot become ready if the bundle changes after runner receipt.
            other_source = new_source()
            assert (
                post(
                    client,
                    endpoint_path,
                    {
                        "run_id": run["id"],
                        "source_id": other_source,
                        "idempotency_key": key,
                    },
                ).status_code
                == 409
            )
            other_proposal = post(
                client,
                endpoint_path,
                {
                    "run_id": run["id"],
                    "source_id": other_source,
                    "idempotency_key": str(uuid4()),
                },
            )
            assert other_proposal.status_code == 201, other_proposal.text
            other_approval = other_proposal.json()["approval"]["id"]
            assert (
                post(
                    client,
                    f"{base}/approvals/{other_approval}/decision",
                    {
                        "decision": "approved",
                    },
                ).status_code
                == 200
            )
            descriptor = acquire_supervisor(supervisor)
            try:
                queued = post(client, f"{endpoint_path}/{other_approval}/dispatch").json()
                attempt2 = queued["attempt_id"]
                second = process_job(spool / "jobs" / f"{attempt2}.json", supervisor)
                assert second is not None and second["snapshot_sha256"] == receipt["snapshot_sha256"]
                bundle = snapshots / second["snapshot_path"]
                bundle.write_bytes(bundle.read_bytes()[:-1] + b" ")
                result = client.get(f"{endpoint_path}/{other_approval}")
                assert result.status_code == 200
                assert result.json()["snapshot_status"] == "unknown"
                assert result.json()["reason_code"] == "receipt_invalid"
            finally:
                os.close(descriptor)
            with create_engine(settings.database_url).connect() as connection:
                count = connection.scalar(
                    text(
                        "SELECT count(*) FROM github_fixture_source_snapshots WHERE project_id=:project_id "
                        "AND approval_id=:approval_id"
                    ),
                    {"project_id": project["id"], "approval_id": other_approval},
                )
            assert count == 0

            # A claimed job without a receipt settles unknown once; a late receipt cannot make it ready.
            missing_source = new_source()
            missing_approval = post(
                client,
                endpoint_path,
                {
                    "run_id": run["id"],
                    "source_id": missing_source,
                    "idempotency_key": str(uuid4()),
                },
            ).json()["approval"]["id"]
            assert (
                post(
                    client,
                    f"{base}/approvals/{missing_approval}/decision",
                    {
                        "decision": "approved",
                    },
                ).status_code
                == 200
            )
            descriptor = acquire_supervisor(supervisor)
            try:
                missing_job = post(client, f"{endpoint_path}/{missing_approval}/dispatch").json()[
                    "attempt_id"
                ]
                claim = spool / "claims" / f"{missing_job}.claim"
                claim.write_text(missing_job)
                old = datetime.now(UTC).timestamp() - 120
                os.utime(claim, (old, old))
                unknown = client.get(f"{endpoint_path}/{missing_approval}")
                assert unknown.status_code == 200
                assert unknown.json()["snapshot_status"] == "unknown"
                assert unknown.json()["reason_code"] == "claimed_receipt_missing"
                (spool / "receipts" / f"{missing_job}.json").write_text("{}")
                assert (
                    client.get(f"{endpoint_path}/{missing_approval}").json()["snapshot_status"] == "unknown"
                )
            finally:
                os.close(descriptor)

            # An approved but expired v3 action cannot dispatch a job.
            dispatch_source = new_source()
            dispatch_approval = post(
                client,
                endpoint_path,
                {
                    "run_id": run["id"],
                    "source_id": dispatch_source,
                    "idempotency_key": str(uuid4()),
                },
            ).json()["approval"]["id"]
            assert (
                post(
                    client,
                    f"{base}/approvals/{dispatch_approval}/decision",
                    {
                        "decision": "approved",
                    },
                ).status_code
                == 200
            )
            descriptor = acquire_supervisor(supervisor)
            try:
                with patch("onefoundry.github_fixture_snapshot_dispatch.datetime") as clock:
                    clock.now.return_value = datetime.now(UTC) + timedelta(hours=1)
                    assert post(client, f"{endpoint_path}/{dispatch_approval}/dispatch").status_code == 409
            finally:
                os.close(descriptor)

            # Expiry and source deletion retain their v2 semantics in the new approval action.
            stale_source = new_source()
            stale = post(
                client,
                endpoint_path,
                {
                    "run_id": run["id"],
                    "source_id": stale_source,
                    "idempotency_key": str(uuid4()),
                },
            ).json()["approval"]["id"]
            assert client.delete(f"{base}/sources/{stale_source}", headers=ORIGIN).status_code == 204
            assert (
                post(
                    client,
                    f"{base}/approvals/{stale}/decision",
                    {
                        "decision": "approved",
                    },
                ).status_code
                == 409
            )
            expired = post(
                client,
                endpoint_path,
                {
                    "run_id": run["id"],
                    "source_id": new_source(),
                    "idempotency_key": str(uuid4()),
                },
            ).json()["approval"]["id"]
            with patch("onefoundry.tool_gateway.datetime") as clock:
                clock.now.return_value = datetime.now(UTC) + timedelta(hours=1)
                assert (
                    post(
                        client,
                        f"{base}/approvals/{expired}/decision",
                        {
                            "decision": "approved",
                        },
                    ).status_code
                    == 409
                )
