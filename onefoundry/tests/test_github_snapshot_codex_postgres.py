"""API to immutable CAS to one-shot fake Codex receipt, on a real PostgreSQL schema."""

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.settings import Settings
from runner.external_cli import CliRequest, CliResult
from runner.external_launch_journal import record_prelaunch
from runner.github_fixture_snapshot_supervisor import HTTPSFixtureConfig, acquire_supervisor, process_job
from runner.github_snapshot_codex_fixture_proof import AFTER
from runner.github_snapshot_codex_service import process_snapshot_codex_job
from runner.github_snapshot_codex_supervisor import (
    SnapshotCodexConfig,
    fence_orphans,
)
from runner.github_snapshot_codex_supervisor import (
    acquire_supervisor as acquire_code_supervisor,
)
from runner.local_source_stage import StageLimits
from tests.test_github_https_fixture_fetch import COMMIT, TOKEN, fixture
from tests.test_github_snapshot_codex_fixture_proof import runtime_routes

IMAGE = "sha256:" + "a" * 64
ORIGIN = {"origin": "http://localhost:8765"}


def _post(client: TestClient, path: str, payload: dict[str, object] | None = None) -> Any:
    return client.post(path, json=payload, headers=ORIGIN)


def _active_child_docker(name: str, calls: list[tuple[str, ...]]) -> Callable[..., str]:
    def docker_child(*args: str) -> str:
        calls.append(args)
        if args[0] == "ps":
            return name if len([item for item in calls if item[0] == "ps"]) == 1 else ""
        assert args == ("rm", "-f", name)
        return ""

    return docker_child


def test_snapshot_codex_api_binding_idempotency_and_receipt(tmp_path: Path) -> None:
    spool_root = tmp_path / "spool"
    fetch_spool = spool_root / "github-fixture-https-snapshot-v3"
    code_spool = spool_root / "external"
    for spool in (fetch_spool, code_spool):
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
        runner_wall_seconds=60,
        runner_receipt_wait_seconds=91,
    )
    with fixture(tmp_path, runtime_routes()) as (endpoint, cert, _handler):
        fetch = HTTPSFixtureConfig(
            fetch_spool, endpoint, cert, TOKEN, StageLimits(10, 4096, 4096, 4), snapshots
        )
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = _post(client, "/api/projects", {"name": "Snapshot coding " + str(uuid4())}).json()
            base = f"/api/projects/{project['id']}"
            engineer = next(
                item for item in client.get(f"{base}/profiles").json() if item["name"] == "Engineer"
            )

            def ready(title: str) -> dict[str, object]:
                order = cast(
                    dict[str, object],
                    _post(
                        client,
                        f"{base}/work-orders",
                        {"title": title, "instructions": "Repair fixture file", "profile_id": engineer["id"]},
                    ).json(),
                )
                assert _post(client, f"{base}/work-orders/{order['id']}/ready").status_code == 200
                return order

            fetch_order = ready("Fetch source snapshot")
            fetch_run = _post(client, f"{base}/work-orders/{fetch_order['id']}/runs").json()
            account = _post(
                client, f"{base}/github/accounts", {"label": "Saved fake", "token": "dummy-saved-value"}
            ).json()
            with patch("onefoundry.source_store.resolve_revision", return_value=("main", COMMIT)):
                source_response = _post(
                    client,
                    f"{base}/sources/github",
                    {"account_id": account["id"], "owner": "sample", "repo": "project", "branch": "main"},
                )
            assert source_response.status_code == 201, source_response.text
            source = source_response.json()
            endpoint_path = f"{base}/tools/github/fixture-snapshot-v3"
            proposal = _post(
                client,
                endpoint_path,
                {"run_id": fetch_run["id"], "source_id": source["id"], "idempotency_key": str(uuid4())},
            )
            assert proposal.status_code == 201, proposal.text
            approval = proposal.json()["approval"]["id"]
            assert (
                _post(client, f"{base}/approvals/{approval}/decision", {"decision": "approved"}).status_code
                == 200
            )
            lock = acquire_supervisor(fetch)
            try:
                launched_fetch = _post(client, f"{endpoint_path}/{approval}/dispatch")
                assert launched_fetch.status_code == 200, launched_fetch.text
                snapshot_id = launched_fetch.json()["attempt_id"]
                fetch_receipt = process_job(fetch_spool / "jobs" / f"{snapshot_id}.json", fetch)
            finally:
                os.close(lock)
            assert fetch_receipt is not None and fetch_receipt["status"] == "done"
            assert client.get(f"{endpoint_path}/{approval}").json()["snapshot_status"] == "done"

            order = ready("Repair approved snapshot")
            capability = {
                "schema_version": 1,
                "observed_at": datetime.now(UTC).isoformat(),
                "available": True,
                "synthetic_only": True,
                "brokered": True,
                "isolated": True,
            }
            (code_spool / "snapshot_codex_capabilities.json").write_text(json.dumps(capability))
            code_config = SnapshotCodexConfig(code_spool, snapshots, IMAGE, IMAGE, 91)
            code_lock = acquire_code_supervisor(code_config)
            launch_path = f"{base}/work-orders/{order['id']}/github-snapshot-codex/runs"
            generic = client.get(f"{base}/work-orders/{order['id']}/external-code/eligibility")
            assert generic.status_code == 200
            assert generic.json()["eligible"] is False
            assert all(not adapter["available"] for adapter in generic.json()["adapters"])
            eligibility = client.get(f"{base}/work-orders/{order['id']}/github-snapshot-codex/eligibility")
            assert eligibility.status_code == 200, eligibility.text
            assert eligibility.json()["eligible"] is True
            assert eligibility.json()["snapshots"][0]["snapshot_attempt_id"] == snapshot_id
            key = str(uuid4())
            payload = {"snapshot_attempt_id": snapshot_id, "idempotency_key": key}
            queued = _post(client, launch_path, payload)
            assert queued.status_code == 201, queued.text
            detail = queued.json()
            assert detail["attempts"][0]["snapshot_sha256"] == fetch_receipt["snapshot_sha256"]
            assert detail["attempts"][0]["snapshot_revision"] == COMMIT
            assert _post(client, launch_path, payload).json()["run"]["id"] == detail["run"]["id"]
            assert (
                _post(client, launch_path, {**payload, "snapshot_attempt_id": str(uuid4())}).status_code
                == 409
            )
            assert _post(client, launch_path, {**payload, "idempotency_key": str(uuid4())}).status_code == 409
            assert (
                _post(
                    client,
                    f"{base}/work-orders/{fetch_order['id']}/github-snapshot-codex/runs",
                    {**payload, "idempotency_key": str(uuid4())},
                ).status_code
                == 409
            )

            def fake_invoke(request: object, _image: str, _fixture_image: str, _attempt_id: str) -> CliResult:
                from runner.external_cli import CliRequest

                assert isinstance(request, CliRequest)
                (request.repository / "file.txt").write_bytes(AFTER)
                log = (
                    f"synthetic_cli=codex_cli model=gpt-5 command=snapshot_file_repair "
                    f"image_sha256={IMAGE} prompt_sha256={'f' * 64} "
                    "allowed_provider_requests=2 blocked_other_connects=3\n"
                ).encode()
                return CliResult("done", "provider_completed", "fake-session", 28, None, log)

            with patch("runner.github_snapshot_codex_service.invoke_file_codex", fake_invoke):
                worker_receipt = process_snapshot_codex_job(
                    code_spool / "jobs" / f"{detail['attempts'][0]['id']}.json",
                    code_spool,
                    snapshots,
                    IMAGE,
                    IMAGE,
                )
            assert worker_receipt is not None and worker_receipt["status"] == "done"
            result = _post(client, f"{base}/runs/{detail['run']['id']}/external-code/reconcile")
            assert result.status_code == 200, result.text
            attempt = result.json()["attempts"][0]
            assert attempt["status"] == "done"
            assert attempt["snapshot_sha256"] == detail["attempts"][0]["snapshot_sha256"]
            assert attempt["baseline_commit"] != attempt["snapshot_revision"]
            assert attempt["commit"] != attempt["baseline_commit"]
            assert attempt["tokens_used"] == 28 and attempt["cost_usd"] is None
            with create_engine(settings.database_url).connect() as connection:
                events = cast(
                    list[str],
                    connection.execute(
                        text(
                            "SELECT event_type FROM events WHERE project_id=:project_id AND run_id=:run_id "
                            "AND event_type IN ('external_code_prelaunch','external_code_session')"
                        ),
                        {"project_id": project["id"], "run_id": detail["run"]["id"]},
                    )
                    .scalars()
                    .all(),
                )
            assert sorted(events) == ["external_code_prelaunch", "external_code_session"]
            repeated = _post(client, f"{base}/runs/{detail['run']['id']}/external-code/reconcile")
            assert repeated.json()["attempts"][0]["status"] == "done"

            wrong_project = _post(client, "/api/projects", {"name": "Other coding project"}).json()
            wrong_base = f"/api/projects/{wrong_project['id']}"
            wrong_engineer = next(
                item for item in client.get(f"{wrong_base}/profiles").json() if item["name"] == "Engineer"
            )
            wrong_order = _post(
                client,
                f"{wrong_base}/work-orders",
                {
                    "title": "Foreign snapshot must be rejected",
                    "instructions": "Repair fixture file",
                    "profile_id": wrong_engineer["id"],
                },
            ).json()
            assert _post(client, f"{wrong_base}/work-orders/{wrong_order['id']}/ready").status_code == 200
            jobs_before = len(list((code_spool / "jobs").glob("*.json")))
            assert (
                _post(
                    client,
                    f"{wrong_base}/work-orders/{wrong_order['id']}/github-snapshot-codex/runs",
                    {"snapshot_attempt_id": snapshot_id, "idempotency_key": str(uuid4())},
                ).status_code
                == 404
            )
            assert len(list((code_spool / "jobs").glob("*.json"))) == jobs_before
            wrong_orders = client.get(f"{wrong_base}/work-orders").json()
            assert next(item for item in wrong_orders if item["id"] == wrong_order["id"])["status"] == "ready"
            with create_engine(settings.database_url).connect() as connection:
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM external_code_attempts WHERE project_id=:project_id"),
                        {"project_id": wrong_project["id"]},
                    )
                    == 0
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM events WHERE project_id=:project_id "
                            "AND event_type='external_code_queued'"
                        ),
                        {"project_id": wrong_project["id"]},
                    )
                    == 0
                )

            concurrent_orders = [ready("Concurrent A"), ready("Concurrent B")]
            shared_key = str(uuid4())

            def concurrent_launch(item: dict[str, object]) -> int:
                response = _post(
                    client,
                    f"{base}/work-orders/{item['id']}/github-snapshot-codex/runs",
                    {"snapshot_attempt_id": snapshot_id, "idempotency_key": shared_key},
                )
                return cast(int, response.status_code)

            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = sorted(pool.map(concurrent_launch, concurrent_orders))
            assert statuses == [201, 409]
            assert len(list((code_spool / "jobs").glob("*.json"))) == 2

            orphan_order = ready("Unknown claim remains terminal")
            orphan = _post(
                client,
                f"{base}/work-orders/{orphan_order['id']}/github-snapshot-codex/runs",
                {"snapshot_attempt_id": snapshot_id, "idempotency_key": str(uuid4())},
            )
            assert orphan.status_code == 201, orphan.text
            orphan_id = orphan.json()["attempts"][0]["id"]
            orphan_run = orphan.json()["run"]["id"]
            claim = code_spool / "claims" / f"{orphan_id}.claim"
            claim.write_text(orphan_id)
            old = datetime.now(UTC).timestamp() - 120
            os.utime(claim, (old, old))
            orphan_reconcile = f"{base}/runs/{orphan_run}/external-code/reconcile"
            assert _post(client, orphan_reconcile).json()["attempts"][0]["status"] == "queued"
            with patch("runner.github_snapshot_codex_supervisor.egress._docker", return_value=""):
                assert fence_orphans(code_config) == 1
            assert _post(client, orphan_reconcile).json()["attempts"][0]["status"] == "unknown"
            (code_spool / "receipts" / f"{orphan_id}.json").write_text("{}")
            assert _post(client, orphan_reconcile).json()["attempts"][0]["status"] == "unknown"

            for corrupt_kind in ("journal", "receipt", "legacy_kind"):
                corrupt_order = ready(f"Corrupt {corrupt_kind} waits for child fence")
                corrupt = _post(
                    client,
                    f"{base}/work-orders/{corrupt_order['id']}/github-snapshot-codex/runs",
                    {"snapshot_attempt_id": snapshot_id, "idempotency_key": str(uuid4())},
                )
                assert corrupt.status_code == 201, corrupt.text
                corrupt_id = corrupt.json()["attempts"][0]["id"]
                corrupt_run = corrupt.json()["run"]["id"]
                corrupt_claim = code_spool / "claims" / f"{corrupt_id}.claim"
                corrupt_claim.write_text(corrupt_id)
                os.utime(corrupt_claim, (old, old))
                if corrupt_kind == "journal":
                    (code_spool / "claims" / f"{corrupt_id}.launch.json").write_text("{")
                elif corrupt_kind == "legacy_kind":
                    manifest = json.loads((code_spool / "jobs" / f"{corrupt_id}.json").read_text())
                    record_prelaunch(
                        code_spool,
                        manifest,
                        CliRequest(
                            "codex_cli",
                            Path("/usr/bin/true"),
                            tmp_path,
                            tmp_path,
                            "synthetic prompt",
                            30,
                            16384,
                        ),
                        "a" * 40,
                    )
                else:
                    (code_spool / "receipts" / f"{corrupt_id}.json").write_text("{}")
                corrupt_reconcile = f"{base}/runs/{corrupt_run}/external-code/reconcile"
                assert _post(client, corrupt_reconcile).json()["attempts"][0]["status"] == "queued"
                suffix = corrupt_id.replace("-", "")
                child_name = f"ap-snapshot-cli-{suffix}"
                docker_calls: list[tuple[str, ...]] = []

                with patch(
                    "runner.github_snapshot_codex_supervisor.egress._docker",
                    _active_child_docker(child_name, docker_calls),
                ):
                    assert fence_orphans(code_config) == 1
                assert ("rm", "-f", child_name) in docker_calls
                after_fence = _post(client, corrupt_reconcile)
                assert after_fence.json()["attempts"][0]["status"] == "unknown"
                assert after_fence.json()["attempts"][0]["reason_code"] == (
                    "receipt_invalid" if corrupt_kind == "receipt" else "launch_record_invalid"
                )

            stale_order = ready("Unavailable source rejects admission")
            stale_path = f"{base}/work-orders/{stale_order['id']}/github-snapshot-codex/runs"
            saved_secret = ModelSecretStore(str(secrets / "github"))
            saved_value = saved_secret.read(UUID(account["id"]))
            saved_secret.remove(UUID(account["id"]))
            assert (
                _post(
                    client,
                    stale_path,
                    {"snapshot_attempt_id": snapshot_id, "idempotency_key": str(uuid4())},
                ).status_code
                == 409
            )
            saved_secret.write(UUID(account["id"]), saved_value)
            removed = client.delete(f"{base}/sources/{source['id']}", headers=ORIGIN)
            assert removed.status_code == 204, removed.text
            assert (
                _post(
                    client,
                    stale_path,
                    {"snapshot_attempt_id": snapshot_id, "idempotency_key": str(uuid4())},
                ).status_code
                == 404
            )
            os.close(code_lock)
            stopped_order = ready("Stopped worker rejects admission")
            stopped_path = f"{base}/work-orders/{stopped_order['id']}/github-snapshot-codex/runs"
            assert (
                _post(
                    client,
                    stopped_path,
                    {"snapshot_attempt_id": snapshot_id, "idempotency_key": str(uuid4())},
                ).status_code
                == 503
            )
