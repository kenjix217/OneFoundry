"""One-shot offline GitHub stage supervisor uses a synthetic local remote only."""

import json
import os
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from onefoundry.github_stage_contract import StageReceipt, manifest_payload
from onefoundry.runner_spool import RunnerSpool
from runner.github_stage_supervisor import OfflineConfig, SupervisorError, acquire_supervisor, process_job
from runner.local_source_stage import StageLimits


def git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - synthetic local Git fixture only
        ["/usr/bin/git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def fixture(tmp_path: Path) -> tuple[Path, str, str]:
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


def spool_fixture(tmp_path: Path) -> Path:
    spool = tmp_path / "spool"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    return spool


def manifest(revision: str) -> dict[str, object]:
    project_id, source_id, account_id, run_id, approval_id, job_id = (uuid4() for _ in range(6))
    return manifest_payload(
        job_id=job_id,
        project_id=project_id,
        run_id=run_id,
        approval_id=approval_id,
        actor_id="operator",
        proposal_digest="a" * 64,
        source={
            "kind": "github_stage_v1",
            "project_id": str(project_id),
            "source_id": str(source_id),
            "account_id": str(account_id),
            "owner": "sample",
            "repo": "project",
            "branch": "main",
            "saved_revision": revision,
        },
    )


def config(spool: Path, remote: Path, *, owner: str = "sample") -> OfflineConfig:
    return OfflineConfig(spool, remote, owner, "project", StageLimits(10, 4096, 8192, 8), 1_000_000)


def test_one_shot_claim_exact_stage_receipt_and_no_staged_artifact(tmp_path: Path) -> None:
    remote, revision, tree = fixture(tmp_path)
    spool = spool_fixture(tmp_path)
    payload = manifest(revision)
    job_id = payload["job_id"]
    RunnerSpool(str(spool)).write_job(UUID(str(job_id)), payload)
    path = spool / "jobs" / f"{job_id}.json"
    first = process_job(path, config(spool, remote))
    assert first is not None
    assert first["status"] == "done"
    assert first["source_tree"] == tree
    assert first["file_count"] == 1
    assert StageReceipt.model_validate(first).request_sha256 == payload["request_sha256"]
    assert process_job(path, config(spool, remote)) is None
    assert (spool / "claims" / f"{job_id}.claim").read_text() == job_id
    assert json.loads((spool / "receipts" / f"{job_id}.json").read_text()) == first
    assert list(tmp_path.glob("onefoundry-gh-stage-*")) == []


def test_identity_mismatch_consumes_job_as_unknown(tmp_path: Path) -> None:
    remote, revision, _ = fixture(tmp_path)
    (remote / "onefoundry-synthetic-fixture.json").write_text(
        json.dumps(
            {
                "kind": "onefoundry_synthetic_github_fixture_v1",
                "owner": "different",
                "repo": "project",
                "revision": revision,
            }
        )
    )
    spool = spool_fixture(tmp_path)
    payload = manifest(revision)
    job_id = UUID(str(payload["job_id"]))
    RunnerSpool(str(spool)).write_job(job_id, payload)
    result = process_job(spool / "jobs" / f"{job_id}.json", config(spool, remote, owner="different"))
    assert result is not None and result["status"] == "unknown"
    assert result["source_tree"] is None
    assert process_job(spool / "jobs" / f"{job_id}.json", config(spool, remote, owner="different")) is None


def test_tampered_digest_rejected_before_claim(tmp_path: Path) -> None:
    remote, revision, _ = fixture(tmp_path)
    spool = spool_fixture(tmp_path)
    payload = manifest(revision)
    job_id = payload["job_id"]
    payload["request_sha256"] = "0" * 64
    path = spool / "jobs" / f"{job_id}.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(SupervisorError, match="stage_manifest_invalid"):
        process_job(path, config(spool, remote))
    assert not (spool / "claims" / f"{job_id}.claim").exists()


def test_unmarked_remote_rejected_before_claim(tmp_path: Path) -> None:
    remote, revision, _ = fixture(tmp_path)
    (remote / "onefoundry-synthetic-fixture.json").unlink()
    spool = spool_fixture(tmp_path)
    payload = manifest(revision)
    job_id = UUID(str(payload["job_id"]))
    RunnerSpool(str(spool)).write_job(job_id, payload)
    with pytest.raises(SupervisorError, match="offline_fixture_marker_invalid"):
        process_job(spool / "jobs" / f"{job_id}.json", config(spool, remote))
    assert not (spool / "claims" / f"{job_id}.claim").exists()


def test_only_one_live_supervisor_can_hold_offline_spool(tmp_path: Path) -> None:
    remote, _, _ = fixture(tmp_path)
    offline = config(spool_fixture(tmp_path), remote)
    descriptor = acquire_supervisor(offline)
    try:
        with pytest.raises(SupervisorError, match="offline_supervisor_already_running"):
            acquire_supervisor(offline)
    finally:
        os.close(descriptor)
    replacement = acquire_supervisor(offline)
    os.close(replacement)
