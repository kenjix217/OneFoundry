"""Approved GitHub staging dispatches once to an offline synthetic supervisor."""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.settings import Settings
from runner.github_stage_supervisor import OfflineConfig, acquire_supervisor, process_job
from runner.local_source_stage import StageLimits

ORIGIN = {"origin": "http://localhost:8765"}
TOKEN = "synthetic-token-never-sent-to-stage-supervisor"


def post(client: TestClient, path: str, payload: dict[str, object] | None = None) -> Response:
    return client.post(path, json=payload, headers=ORIGIN)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - synthetic local Git fixture only
        ["/usr/bin/git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def fixture_remote(tmp_path: Path) -> tuple[Path, str, str]:
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.name", "Fixture")
    git(work, "config", "user.email", "fixture@example.invalid")
    (work / "file.txt").write_text("saved\n")
    git(work, "add", "file.txt")
    git(work, "commit", "-qm", "baseline")
    remote = tmp_path / "remote.git"
    subprocess.run(["/usr/bin/git", "init", "--bare", "-q", str(remote)], check=True)  # noqa: S603
    git(work, "push", "-q", str(remote), "main:main")
    revision = git(work, "rev-parse", "HEAD")
    (remote / "onefoundry-synthetic-fixture.json").write_text(
        json.dumps(
            {
                "kind": "onefoundry_synthetic_github_fixture_v1",
                "owner": "sample",
                "repo": "project",
                "revision": revision,
            }
        )
    )
    return remote, revision, git(work, "rev-parse", "HEAD^{tree}")


def test_approved_stage_one_shot_success_unknown_and_stale_source(tmp_path: Path) -> None:
    remote, revision, tree = fixture_remote(tmp_path)
    spool_root = tmp_path / "spool"
    stage_spool = spool_root / "github-stage"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (stage_spool / name).mkdir(parents=True)
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
    supervisor = OfflineConfig(
        stage_spool, remote, "sample", "project", StageLimits(10, 4096, 8192, 8), 1_000_000
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = post(client, "/api/projects", {"name": "Offline stage dispatch"}).json()
        path = f"/api/projects/{project['id']}"
        profile = next(item for item in client.get(f"{path}/profiles").json() if item["name"] == "Engineer")
        order = post(
            client,
            f"{path}/work-orders",
            {
                "title": "Offline stage",
                "instructions": "Stage saved source",
                "profile_id": profile["id"],
            },
        ).json()
        assert post(client, f"{path}/work-orders/{order['id']}/ready").status_code == 200
        run = post(client, f"{path}/work-orders/{order['id']}/runs").json()
        account = post(
            client,
            f"{path}/github/accounts",
            {
                "label": "Synthetic account",
                "token": TOKEN,
            },
        ).json()
        endpoint = f"{path}/tools/github/stage-proposals"

        def new_source() -> str:
            with patch("onefoundry.source_store.resolve_revision", return_value=("main", revision)):
                result = post(
                    client,
                    f"{path}/sources/github",
                    {
                        "account_id": account["id"],
                        "owner": "sample",
                        "repo": "project",
                        "branch": "main",
                    },
                )
            assert result.status_code == 201, result.text
            return str(result.json()["id"])

        def new_proposal(source_id: str) -> str:
            result = post(client, endpoint, {"run_id": run["id"], "source_id": source_id})
            assert result.status_code == 201, result.text
            return str(result.json()["approval"]["id"])

        first = new_proposal(new_source())
        assert post(client, f"{endpoint}/{first}/dispatch").status_code == 409
        assert post(client, f"{path}/approvals/{first}/decision", {"decision": "approved"}).status_code == 200
        assert post(client, f"{endpoint}/{first}/dispatch").status_code == 503
        supervisor_descriptor = acquire_supervisor(supervisor)
        with patch("onefoundry.source_store.resolve_revision") as no_github_api:
            launched = post(client, f"{endpoint}/{first}/dispatch")
        no_github_api.assert_not_called()
        assert launched.status_code == 200, launched.text
        assert launched.json()["stage_status"] == "queued"
        assert launched.json()["mode"] == "offline_synthetic_fixture"
        attempt = launched.json()["attempt_id"]
        assert post(client, f"{endpoint}/{first}/dispatch").status_code == 409
        job_path = stage_spool / "jobs" / f"{attempt}.json"
        manifest = json.loads(job_path.read_text())
        assert manifest["approval_id"] == first
        assert manifest["source"]["saved_revision"] == revision
        assert TOKEN not in job_path.read_text()
        assert not (stage_spool / "receipts" / f"{attempt}.json").exists()
        receipt = process_job(job_path, supervisor)
        assert receipt is not None and receipt["status"] == "done"
        assert process_job(job_path, supervisor) is None
        status = client.get(f"{endpoint}/{first}")
        assert status.status_code == 200, status.text
        assert status.json()["stage_status"] == "done"
        assert status.json()["source_tree"] == tree
        receipt_path = stage_spool / "receipts" / f"{attempt}.json"
        assert status.json()["receipt_sha256"] == hashlib.sha256(receipt_path.read_bytes()).hexdigest()

        second = new_proposal(new_source())
        assert (
            post(client, f"{path}/approvals/{second}/decision", {"decision": "approved"}).status_code == 200
        )
        waiting = post(client, f"{endpoint}/{second}/dispatch").json()
        second_attempt = waiting["attempt_id"]
        os.close(supervisor_descriptor)  # Supervisor died after queue; never dispatch a second attempt.
        claim = stage_spool / "claims" / f"{second_attempt}.claim"
        claim.write_text(second_attempt)
        old = time.time() - 1000
        os.utime(claim, (old, old))
        unknown = client.get(f"{endpoint}/{second}")
        assert unknown.status_code == 200, unknown.text
        assert unknown.json()["stage_status"] == "unknown"
        assert unknown.json()["reason_code"] == "claimed_receipt_missing"
        assert post(client, f"{endpoint}/{second}/dispatch").status_code == 409
        assert process_job(stage_spool / "jobs" / f"{second_attempt}.json", supervisor) is None

        third_source = new_source()
        third = new_proposal(third_source)
        assert post(client, f"{path}/approvals/{third}/decision", {"decision": "approved"}).status_code == 200
        assert client.delete(f"{path}/sources/{third_source}", headers=ORIGIN).status_code == 204
        supervisor_descriptor = acquire_supervisor(supervisor)
        assert post(client, f"{endpoint}/{third}/dispatch").status_code == 409
        os.close(supervisor_descriptor)
        assert client.get(f"{endpoint}/{third}").json()["stage_status"] == "not_launched"
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            receipt_events = connection.scalar(
                text(
                    "SELECT count(*) FROM events WHERE project_id=:project_id "
                    "AND event_type='github_stage_receipt' AND payload->>'approval_id'=:approval_id"
                ),
                {"project_id": UUID(project["id"]), "approval_id": first},
            )
            unknown_events = connection.scalar(
                text(
                    "SELECT count(*) FROM events WHERE project_id=:project_id "
                    "AND event_type='github_stage_unknown' AND payload->>'approval_id'=:approval_id"
                ),
                {"project_id": UUID(project["id"]), "approval_id": second},
            )
        engine.dispose()
        assert receipt_events == 1 and unknown_events == 1
