"""Approval-bound, one-shot local HTTPS verification; no live GitHub request."""

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.settings import Settings
from runner.github_fixture_https_supervisor import HTTPSFixtureConfig, acquire_supervisor, process_job
from runner.local_source_stage import StageLimits
from tests.test_github_https_fixture_fetch import COMMIT, TOKEN, TREE, fixture

ORIGIN = {"origin": "http://localhost:8765"}
ACCOUNT_TOKEN = "saved-account-token-must-never-reach-fixture"


def post(client: TestClient, path: str, payload: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
    return client.post(path, json=payload, headers=ORIGIN)


def test_exact_v2_approval_dispatch_receipt_and_old_approval_separation(tmp_path: Path) -> None:
    spool_root = tmp_path / "spool"
    spool = spool_root / "github-fixture-https-v2"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
        runner_spool_root=str(spool_root),
        runner_wall_seconds=1,
        runner_receipt_wait_seconds=31,
    )
    with fixture(tmp_path) as (endpoint, cert, handler):
        supervisor = HTTPSFixtureConfig(spool, endpoint, cert, TOKEN, StageLimits(10, 4096, 4096, 4))
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = post(client, "/api/projects", {"name": "HTTPS fixture v2"}).json()
            base = f"/api/projects/{project['id']}"
            profile = next(
                item for item in client.get(f"{base}/profiles").json() if item["name"] == "Engineer"
            )
            order = post(
                client,
                f"{base}/work-orders",
                {
                    "title": "Verify fixture",
                    "instructions": "Verify selected source",
                    "profile_id": profile["id"],
                },
            ).json()
            assert post(client, f"{base}/work-orders/{order['id']}/ready").status_code == 200
            run = post(client, f"{base}/work-orders/{order['id']}/runs").json()
            account = post(
                client,
                f"{base}/github/accounts",
                {
                    "label": "Saved account",
                    "token": ACCOUNT_TOKEN,
                },
            ).json()
            with patch("onefoundry.source_store.resolve_revision", return_value=("main", COMMIT)):
                source = post(
                    client,
                    f"{base}/sources/github",
                    {
                        "account_id": account["id"],
                        "owner": "sample",
                        "repo": "project",
                        "branch": "main",
                    },
                ).json()
            v1 = post(
                client,
                f"{base}/tools/github/stage-proposals",
                {
                    "run_id": run["id"],
                    "source_id": source["id"],
                },
            ).json()["approval"]["id"]
            assert (
                post(client, f"{base}/approvals/{v1}/decision", {"decision": "approved"}).status_code == 200
            )
            endpoint = f"{base}/tools/github/fixture-fetch-v2"
            assert client.get(f"{endpoint}/{v1}").status_code == 404
            assert post(client, f"{endpoint}/{v1}/dispatch").status_code == 404
            key = str(uuid4())
            payload = {"run_id": run["id"], "source_id": source["id"], "idempotency_key": key}
            with patch("onefoundry.source_store.resolve_revision") as no_live_lookup:
                proposed = post(client, endpoint, payload)
            no_live_lookup.assert_not_called()
            assert proposed.status_code == 201, proposed.text
            approval_id = proposed.json()["approval"]["id"]
            assert proposed.json()["source"]["saved_revision"] == COMMIT
            assert post(client, endpoint, payload).json()["approval"]["id"] == approval_id
            assert post(client, f"{endpoint}/{approval_id}/dispatch").status_code == 409
            assert (
                post(
                    client,
                    f"{base}/approvals/{approval_id}/decision",
                    {
                        "decision": "approved",
                    },
                ).status_code
                == 200
            )
            assert post(client, f"{endpoint}/{approval_id}/dispatch").status_code == 503
            descriptor = acquire_supervisor(supervisor)
            try:
                launched = post(client, f"{endpoint}/{approval_id}/dispatch")
                assert launched.status_code == 200, launched.text
                assert launched.json()["fetch_status"] == "queued"
                assert launched.json()["action"] == "verify_local_https_fixture"
                attempt_id = launched.json()["attempt_id"]
                assert post(client, f"{endpoint}/{approval_id}/dispatch").status_code == 409
                job = spool / "jobs" / f"{attempt_id}.json"
                assert job.exists() and ACCOUNT_TOKEN not in job.read_text() and TOKEN not in job.read_text()
                receipt = process_job(job, supervisor)
                assert receipt is not None and receipt["status"] == "done"
                assert receipt["reason_code"] == "verified_only"
                assert process_job(job, supervisor) is None
                readback = client.get(f"{endpoint}/{approval_id}")
                assert readback.status_code == 200, readback.text
                assert readback.json()["fetch_status"] == "done"
                assert readback.json()["source_revision"] == COMMIT
                assert readback.json()["source_tree"] == TREE
                receipt_path = spool / "receipts" / f"{attempt_id}.json"
                assert (
                    readback.json()["receipt_sha256"] == hashlib.sha256(receipt_path.read_bytes()).hexdigest()
                )
                assert len(handler.observed) == 5
                assert all(auth == "Bearer " + TOKEN for _, auth in handler.observed)
            finally:
                os.close(descriptor)
            with create_engine(settings.database_url).connect() as connection:
                events = (
                    connection.execute(
                        text(
                            "SELECT event_type,payload FROM events WHERE project_id=:project_id "
                            "AND payload->>'approval_id'=:approval_id "
                            "AND event_type LIKE 'github_fixture_fetch_%' ORDER BY created_at,id"
                        ),
                        {"project_id": project["id"], "approval_id": approval_id},
                    )
                    .mappings()
                    .all()
                )
            assert [event["event_type"] for event in events] == [
                "github_fixture_fetch_proposed",
                "github_fixture_fetch_queued",
                "github_fixture_fetch_receipt",
            ]
            assert ACCOUNT_TOKEN not in str(events) and TOKEN not in str(events)
            assert not list(tmp_path.glob("*checkout*"))

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

            def new_proposal(source_id: str) -> str:
                response = post(
                    client,
                    endpoint,
                    {
                        "run_id": run["id"],
                        "source_id": source_id,
                        "idempotency_key": str(uuid4()),
                    },
                )
                assert response.status_code == 201, response.text
                return str(response.json()["approval"]["id"])

            assert (
                post(
                    client,
                    endpoint,
                    {
                        "run_id": run["id"],
                        "source_id": source["id"],
                        "idempotency_key": str(uuid4()),
                    },
                ).status_code
                == 409
            )
            assert (
                post(
                    client,
                    endpoint,
                    {
                        "run_id": run["id"],
                        "source_id": new_source(),
                        "idempotency_key": key,
                    },
                ).status_code
                == 409
            )

            stale_source = new_source()
            stale = new_proposal(stale_source)
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
            assert client.get(f"{endpoint}/{stale}").json()["source_current"] is False

            expired_at = datetime.now(UTC) + timedelta(hours=1)
            expiring = new_proposal(new_source())
            with patch("onefoundry.tool_gateway.datetime") as clock:
                clock.now.return_value = expired_at
                assert (
                    post(
                        client,
                        f"{base}/approvals/{expiring}/decision",
                        {
                            "decision": "approved",
                        },
                    ).status_code
                    == 409
                )
            approved = new_proposal(new_source())
            assert (
                post(
                    client,
                    f"{base}/approvals/{approved}/decision",
                    {
                        "decision": "approved",
                    },
                ).status_code
                == 200
            )
            descriptor = acquire_supervisor(supervisor)
            try:
                with patch("onefoundry.github_fixture_fetch_dispatch.datetime") as clock:
                    clock.now.return_value = expired_at
                    assert post(client, f"{endpoint}/{approved}/dispatch").status_code == 409

                malformed = new_proposal(new_source())
                assert (
                    post(
                        client,
                        f"{base}/approvals/{malformed}/decision",
                        {
                            "decision": "approved",
                        },
                    ).status_code
                    == 200
                )
                dispatched = post(client, f"{endpoint}/{malformed}/dispatch")
                malformed_attempt = dispatched.json()["attempt_id"]
                (spool / "receipts" / f"{malformed_attempt}.json").write_text(
                    '{"schema_version":1,"target_id":"github_stage_offline_v1"}'
                )
                bad_status = client.get(f"{endpoint}/{malformed}")
                assert bad_status.status_code == 200
                assert bad_status.json()["fetch_status"] == "unknown"
                assert bad_status.json()["reason_code"] == "receipt_invalid"
                assert post(client, f"{endpoint}/{malformed}/dispatch").status_code == 409

                missing = new_proposal(new_source())
                assert (
                    post(
                        client,
                        f"{base}/approvals/{missing}/decision",
                        {
                            "decision": "approved",
                        },
                    ).status_code
                    == 200
                )
                missing_attempt = post(client, f"{endpoint}/{missing}/dispatch").json()["attempt_id"]
                claim = spool / "claims" / f"{missing_attempt}.claim"
                claim.write_text(missing_attempt)
                old = time.time() - 1000
                os.utime(claim, (old, old))
                late = client.get(f"{endpoint}/{missing}")
                assert late.status_code == 200
                assert late.json()["fetch_status"] == "unknown"
                assert late.json()["reason_code"] == "claimed_receipt_missing"
                assert post(client, f"{endpoint}/{missing}/dispatch").status_code == 409

                concurrent_source = new_source()
                concurrent_payload = {
                    "run_id": run["id"],
                    "source_id": concurrent_source,
                    "idempotency_key": str(uuid4()),
                }
                with ThreadPoolExecutor(max_workers=2) as pool:
                    responses = list(pool.map(lambda _: post(client, endpoint, concurrent_payload), range(2)))
                assert [response.status_code for response in responses] == [201, 201]
                concurrent_approval = responses[0].json()["approval"]["id"]
                assert responses[1].json()["approval"]["id"] == concurrent_approval
                assert (
                    post(
                        client,
                        f"{base}/approvals/{concurrent_approval}/decision",
                        {
                            "decision": "approved",
                        },
                    ).status_code
                    == 200
                )
                with ThreadPoolExecutor(max_workers=2) as pool:
                    dispatches = list(
                        pool.map(
                            lambda _: post(client, f"{endpoint}/{concurrent_approval}/dispatch"),
                            range(2),
                        )
                    )
                assert sorted(response.status_code for response in dispatches) == [200, 409]
            finally:
                os.close(descriptor)
