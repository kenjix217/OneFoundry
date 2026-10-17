"""V3 fake HTTPS supervisor materializes only exact, approved jobs."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from onefoundry.github_fixture_snapshot_contract import FetchIntent, manifest_payload
from onefoundry.github_snapshot_bundle import read_snapshot
from runner.github_fixture_snapshot_supervisor import HTTPSFixtureConfig, SupervisorError, process_job
from runner.local_source_stage import StageLimits
from tests.test_github_https_fixture_fetch import COMMIT, TOKEN, TREE, fixture


def job(spool: Path, expires_at: str) -> Path:
    project, source, account, run, approval, attempt, key = [uuid4() for _ in range(7)]
    intent = FetchIntent.model_validate(
        {
            "kind": "github_fixture_snapshot_v3",
            "action": "materialize_local_https_fixture_snapshot",
            "project_id": str(project),
            "source_id": str(source),
            "account_id": str(account),
            "owner": "sample",
            "repo": "project",
            "branch": "main",
            "saved_revision": COMMIT,
            "expires_at": expires_at,
            "idempotency_key": str(key),
        }
    )
    manifest = manifest_payload(
        job_id=attempt,
        project_id=project,
        run_id=run,
        approval_id=approval,
        actor_id="operator",
        proposal_digest="a" * 64,
        intent=intent,
    )
    path = spool / "jobs" / f"{attempt}.json"
    path.write_text(json.dumps(manifest))
    return path


def roots(tmp_path: Path) -> tuple[Path, Path]:
    spool = tmp_path / "spool"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    snapshots = tmp_path / "snapshots-root"
    (snapshots / "snapshots").mkdir(parents=True)
    return spool, snapshots


def test_supervisor_writes_bounded_bundle_and_receipt_once(tmp_path: Path) -> None:
    spool, snapshots = roots(tmp_path)
    path = job(spool, (datetime.now(UTC) + timedelta(minutes=5)).isoformat())
    with fixture(tmp_path) as (endpoint, cert, handler):
        config = HTTPSFixtureConfig(spool, endpoint, cert, TOKEN, StageLimits(10, 4096, 4096, 4), snapshots)
        receipt = process_job(path, config)
        assert receipt is not None and receipt["status"] == "done"
        assert receipt["source_tree"] == TREE
        assert receipt["reason_code"] == "snapshot_ready"
        saved = read_snapshot(
            snapshots, receipt["snapshot_path"], receipt["snapshot_sha256"], receipt["snapshot_byte_size"]
        )
        assert saved.source_tree == TREE and len(saved.files) == 1
        assert saved.files[0].path == "file.txt"
        assert process_job(path, config) is None
        assert len(handler.observed) == 5


def test_expired_and_old_jobs_never_materialize(tmp_path: Path) -> None:
    spool, snapshots = roots(tmp_path)
    expired = job(spool, "2000-01-01T00:00:00Z")
    with fixture(tmp_path) as (endpoint, cert, handler):
        config = HTTPSFixtureConfig(spool, endpoint, cert, TOKEN, StageLimits(10, 4096, 4096, 4), snapshots)
        receipt = process_job(expired, config)
        assert receipt is not None and receipt["status"] == "unknown"
        assert receipt["snapshot_path"] is None and handler.observed == []
        assert not list((snapshots / "snapshots").iterdir())
        old = spool / "jobs" / f"{uuid4()}.json"
        old.write_text('{"schema_version":2,"target_id":"github_fixture_https_v2"}')
        with pytest.raises(SupervisorError, match="fixture_manifest_invalid"):
            process_job(old, config)
