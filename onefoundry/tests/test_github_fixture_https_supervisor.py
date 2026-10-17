"""The HTTPS fixture supervisor cannot consume old jobs or contact foreign hosts."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from onefoundry.github_fixture_fetch_contract import FetchIntent, manifest_payload
from runner.github_fixture_https_supervisor import HTTPSFixtureConfig, SupervisorError, process_job
from runner.github_https_fixture_server import prepare
from runner.local_source_stage import StageLimits
from tests.test_github_https_fixture_fetch import COMMIT, TOKEN, fixture


def test_fixture_prepare_reuses_persistent_volume_and_renews_expired_tls(tmp_path: Path) -> None:
    root = tmp_path / "fixture-volume"
    other = tmp_path / "other-volume"
    other.mkdir()
    (other / "db-marker").write_text("preserve")
    prepare(root)
    first = {path.name: path.read_bytes() for path in root.iterdir()}
    prepare(root)
    assert {path.name: path.read_bytes() for path in root.iterdir()} == first
    future = datetime.now(UTC) + timedelta(days=2)
    with patch("runner.github_https_fixture_server.datetime") as clock:
        clock.now.return_value = future
        prepare(root)
        renewed = {path.name: path.read_bytes() for path in root.iterdir()}
        prepare(root)
        assert {path.name: path.read_bytes() for path in root.iterdir()} == renewed
    assert renewed["ca.pem"] != first["ca.pem"]
    assert renewed["server-key.pem"] != first["server-key.pem"]
    assert renewed["dummy-token"] == first["dummy-token"]
    assert (other / "db-marker").read_text() == "preserve"
    (root / "unexpected").write_text("do not remove")
    with pytest.raises(ValueError, match="unexpected files"):
        prepare(root)
    assert (root / "unexpected").read_text() == "do not remove"


def test_expired_intent_is_claimed_once_without_network(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    project, source, account, run, approval, job, key = [uuid4() for _ in range(7)]
    intent = FetchIntent.model_validate(
        {
            "kind": "github_fixture_fetch_v2",
            "action": "verify_local_https_fixture",
            "project_id": str(project),
            "source_id": str(source),
            "account_id": str(account),
            "owner": "sample",
            "repo": "project",
            "branch": "main",
            "saved_revision": COMMIT,
            "expires_at": "2000-01-01T00:00:00Z",
            "idempotency_key": str(key),
        }
    )
    manifest = manifest_payload(
        job_id=job,
        project_id=project,
        run_id=run,
        approval_id=approval,
        actor_id="operator",
        proposal_digest="a" * 64,
        intent=intent,
    )
    path = spool / "jobs" / f"{job}.json"
    path.write_text(json.dumps(manifest))
    with fixture(tmp_path) as (endpoint, cert, handler):
        config = HTTPSFixtureConfig(spool, endpoint, cert, TOKEN, StageLimits(10, 4096, 4096, 4))
        receipt = process_job(path, config)
        assert receipt is not None and receipt["status"] == "unknown"
        assert receipt["reason_code"] == "approval_expired"
        assert process_job(path, config) is None
        assert handler.observed == []
        assert not list(tmp_path.glob("*checkout*"))


def test_old_manifest_and_foreign_endpoint_are_rejected(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    cert = tmp_path / "cert.pem"
    cert.write_text("invalid certificate")
    with pytest.raises(SupervisorError, match="fixture_https_config_invalid"):
        HTTPSFixtureConfig(
            spool, "https://api.github.com", cert, TOKEN, StageLimits(10, 4096, 4096, 4)
        ).validate()
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    with fixture(fixture_root) as (endpoint, valid_cert, _):
        old_job = spool / "jobs" / f"{uuid4()}.json"
        old_job.write_text('{"schema_version":1,"target_id":"github_stage_offline_v1"}')
        config = HTTPSFixtureConfig(spool, endpoint, valid_cert, TOKEN, StageLimits(10, 4096, 4096, 4))
        with pytest.raises(SupervisorError, match="fixture_manifest_invalid"):
            process_job(old_job, config)
