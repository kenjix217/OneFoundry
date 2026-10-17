"""Real OS-isolated offline proof; no coding provider or account is used."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from onefoundry.external_code_schemas import ExternalCodeReceipt
from runner.local_source_probe import ProbeConfig, process_probe_job, write_probe_capabilities
from runner.local_source_stage import StageLimits


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - synthetic test repository only
        ["/usr/bin/git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _setup(tmp_path: Path) -> tuple[ProbeConfig, Path, Path, dict[str, object]]:
    spool = tmp_path / "spool"
    root = tmp_path / "source"
    repo = root / "repo"
    repo.mkdir(parents=True)
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "tracked.txt").write_text("committed\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "baseline")
    attempt_id = str(uuid4())
    source = {
        "source_id": str(uuid4()),
        "root_id": "workspace",
        "relative_path": "repo",
        "branch": "main",
        "revision": _git(repo, "rev-parse", "HEAD"),
    }
    job: dict[str, object] = {
        "schema_version": 2,
        "job_id": attempt_id,
        "project_id": str(uuid4()),
        "run_id": str(uuid4()),
        "attempt_id": attempt_id,
        "adapter_id": "offline_probe",
        "target_id": "local_source_probe_v1",
        "stage": "engineer",
        "actor_id": "operator",
        "task_title": "Offline proof",
        "task_instructions": "Add a marker",
        "limits": {"wall_seconds": 10, "max_log_bytes": 4096, "max_diff_bytes": 4096},
        "source": source,
    }
    path = spool / "jobs" / f"{attempt_id}.json"
    _write_job(path, job)
    return ProbeConfig(spool, root, Path("/usr/bin/bwrap"), StageLimits(10, 1024, 4096, 5)), path, repo, job


def _write_job(path: Path, job: dict[str, object]) -> None:
    unsigned = {key: value for key, value in job.items() if key != "request_sha256"}
    job["request_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path.write_text(json.dumps(job))


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_real_bwrap_child_isolated_and_candidate_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, path, repo, job = _setup(tmp_path)
    source = job["source"]
    assert isinstance(source, dict)
    revision = source["revision"]
    assert isinstance(revision, str)
    monkeypatch.setenv("ONEFOUNDRY_TEST_SECRET", "must-not-enter-child")
    import runner.local_source_probe as probe

    original_child = probe._child

    def inspect_then_launch(
        staged: Path,
        attempt_id: str,
        cfg: ProbeConfig,
        deadline: float,
        max_log: int,
        parent_net: str,
    ) -> bytes:
        assert _git(staged, "rev-list", "--count", "HEAD") == "1"
        assert _git(staged, "rev-parse", "HEAD") != revision
        missing_source_object = subprocess.run(  # noqa: S603 - synthetic Git probe
            ["/usr/bin/git", "-C", str(staged), "cat-file", "-e", revision],
            capture_output=True,
            check=False,
        )
        assert missing_source_object.returncode != 0
        return original_child(staged, attempt_id, cfg, deadline, max_log, parent_net)

    monkeypatch.setattr(probe, "_child", inspect_then_launch)
    receipt = process_probe_job(path, config)
    assert receipt is not None
    ExternalCodeReceipt.model_validate(receipt)
    assert receipt["status"] == "done", receipt
    assert receipt["baseline_commit"] != revision
    assert receipt["source"]["revision"] == revision
    assert receipt["branch"] == f"onefoundry/probe-{job['attempt_id']}"
    assert receipt["tests"] == [{"name": "git_diff_check", "status": "passed", "exit_code": 0}]
    assert receipt["provider_session_id"] is None and receipt["tokens_used"] == 0
    assert receipt["cost_usd"] == "0"
    diff = (config.spool / receipt["artifacts"][0]["path"]).read_bytes()
    log = (config.spool / receipt["artifacts"][1]["path"]).read_bytes()
    assert b"OneFoundry offline source probe" in diff
    assert b"must-not-enter-child" not in diff
    assert b"ONEFOUNDRY_TEST_SECRET" not in log
    assert b'"root_entries":["bin","dev"' in log
    assert b'"network_connect":"blocked"' in log
    assert hashlib.sha256(diff).hexdigest() == receipt["diff_sha256"]
    assert (config.spool / "claims" / f"{job['attempt_id']}.claim").exists()
    assert process_probe_job(path, config) is None
    assert _git(repo, "status", "--porcelain") == ""


def test_source_drift_is_definitive_failed_before_child(tmp_path: Path) -> None:
    config, path, repo, job = _setup(tmp_path)
    (repo / "tracked.txt").write_text("new commit\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "advance")
    receipt = process_probe_job(path, config)
    assert receipt is not None
    ExternalCodeReceipt.model_validate(receipt)
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "source_revision_drift"
    assert receipt["branch"] is None and receipt["artifacts"] == []
    assert process_probe_job(path, config) is None


def test_claimed_missing_receipt_does_not_retry(tmp_path: Path) -> None:
    config, path, _, job = _setup(tmp_path)
    (config.spool / "claims" / f"{job['attempt_id']}.claim").write_text("claimed")
    assert process_probe_job(path, config) is None
    assert list((config.spool / "receipts").iterdir()) == []


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_preclaimed_exchange_requires_exact_claim_and_starts_once(tmp_path: Path) -> None:
    config, path, _, job = _setup(tmp_path)
    claim = config.spool / "claims" / f"{job['attempt_id']}.claim"
    with pytest.raises(ValueError, match="invalid_probe_claim"):
        process_probe_job(path, config, preclaimed=True)
    claim.write_text("wrong")
    with pytest.raises(ValueError, match="invalid_probe_claim"):
        process_probe_job(path, config, preclaimed=True)
    claim.write_text(str(job["attempt_id"]))
    first = process_probe_job(path, config, preclaimed=True)
    assert first is not None and first["status"] == "done"
    (config.spool / "receipts" / f"{job['attempt_id']}.json").unlink()
    assert process_probe_job(path, config, preclaimed=True) is None
    assert (config.spool / "claims" / f"{job['attempt_id']}.started").exists()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_two_supervisors_claim_once(tmp_path: Path) -> None:
    config, path, _, job = _setup(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: process_probe_job(path, config), range(2)))
    assert len([item for item in outcomes if item is not None]) == 1
    assert list((config.spool / "claims").iterdir()) == [
        config.spool / "claims" / f"{job['attempt_id']}.claim"
    ]
    assert len(list((config.spool / "receipts").iterdir())) == 1


def test_drift_during_preparation_fails_before_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, path, source_repo, _ = _setup(tmp_path)
    import runner.local_source_probe as probe

    original_prepare = probe._prepare_repo

    def advance_source(staged: Path, attempt_id: str, tree: str, deadline: float) -> str:
        baseline = original_prepare(staged, attempt_id, tree, deadline)
        (source_repo / "tracked.txt").write_text("advanced during preparation\n")
        _git(source_repo, "add", "tracked.txt")
        _git(source_repo, "commit", "-qm", "advance")
        return baseline

    monkeypatch.setattr(probe, "_prepare_repo", advance_source)
    receipt = process_probe_job(path, config)
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "source_revision_drift"


def test_source_path_swap_during_preparation_never_reads_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, path, source_repo, _ = _setup(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("private fixture\n")
    import runner.local_source_probe as probe

    original_prepare = probe._prepare_repo

    def swap_source(staged: Path, attempt_id: str, tree: str, deadline: float) -> str:
        baseline = original_prepare(staged, attempt_id, tree, deadline)
        source_repo.rename(source_repo.with_name("repo-moved"))
        source_repo.symlink_to(outside, target_is_directory=True)
        return baseline

    monkeypatch.setattr(probe, "_prepare_repo", swap_source)
    receipt = process_probe_job(path, config)
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["reason_code"] == "source_path_unavailable"
    assert (outside / "private.txt").read_text() == "private fixture\n"


def test_post_launch_missing_evidence_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, path, _, _ = _setup(tmp_path)
    import runner.local_source_probe as probe

    def lost_output(*_args: object, **_kwargs: object) -> bytes:
        raise TimeoutError

    monkeypatch.setattr(probe, "_child", lost_output)
    receipt = process_probe_job(path, config)
    assert receipt is not None
    assert receipt["status"] == "unknown"
    assert receipt["reason_code"] == "probe_failure_after_child"
    assert receipt["branch"] is None and receipt["artifacts"] == []


def test_tampered_manifest_rejected_before_claim(tmp_path: Path) -> None:
    config, path, _, job = _setup(tmp_path)
    job["task_title"] = "tampered"
    path.write_text(json.dumps(job))
    with pytest.raises(ValueError, match="invalid_probe_digest"):
        process_probe_job(path, config)
    assert list((config.spool / "claims").iterdir()) == []


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_capability_requires_real_bwrap_selftest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _, _, _ = _setup(tmp_path)
    assert write_probe_capabilities(config)
    path = config.spool / "probe_capabilities.json"
    capability = json.loads(path.read_text())
    assert capability["available"] is True
    assert capability["isolated"] is True
    assert capability["source_root_ready"] is True
    assert not (config.spool / "capabilities.json").exists()
    import runner.local_source_probe as probe

    def denied(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("namespace unavailable")

    monkeypatch.setattr(probe, "_child", denied)
    assert not write_probe_capabilities(config)
    unavailable = json.loads(path.read_text())
    assert unavailable["available"] is False
    assert unavailable["reason"] == "isolation_unavailable"
