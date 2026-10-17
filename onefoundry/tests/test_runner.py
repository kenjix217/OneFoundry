"""Real offline Git/QA/merge checks for the separate fixture runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from runner.contract import ContractError, Job
from runner.service import process_job, run_pending


def _manifest(
    spool: Path,
    stage: str = "engineer",
    *,
    project_id: str | None = None,
    run_id: str | None = None,
    source: dict[str, object] | None = None,
    approval: bool = False,
    wall_seconds: int = 30,
) -> Path:
    job_id = str(uuid4())
    payload: dict[str, object] = {
        "schema_version": 1,
        "job_id": job_id,
        "project_id": project_id or str(uuid4()),
        "run_id": run_id or str(uuid4()),
        "fixture_id": "engineer_qa_v1",
        "stage": stage,
        "actor_id": f"profile:{uuid4()}",
        "source_job_id": source["job_id"] if source else None,
        "approval_id": str(uuid4()) if approval else None,
        "expected_branch": source["branch"] if source else None,
        "expected_commit": source["commit"] if source else None,
        "expected_diff_sha256": source["diff_sha256"] if source else None,
        "expected_baseline_commit": source["baseline_commit"] if source else None,
        "limits": {
            "wall_seconds": wall_seconds,
            "max_cost_usd": 0,
            "max_log_bytes": 16384,
            "max_diff_bytes": 32768,
        },
    }
    path = spool / "jobs" / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _receipt(path: Path, spool: Path) -> dict[str, object]:
    result = process_job(path, spool)
    assert result is not None
    saved = json.loads((spool / "receipts" / path.name).read_text(encoding="utf-8"))
    assert saved == result
    return result


def _cycle(spool: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    project_id, run_id = str(uuid4()), str(uuid4())
    engineer = _receipt(_manifest(spool, project_id=project_id, run_id=run_id), spool)
    assert engineer["status"] == "done"
    qa = _receipt(_manifest(spool, "qa", project_id=project_id, run_id=run_id, source=engineer), spool)
    merge = _receipt(
        _manifest(spool, "merge", project_id=project_id, run_id=run_id, source=engineer, approval=True),
        spool,
    )
    return engineer, qa, merge


def test_real_failing_test_scripted_repair_qa_and_approved_merge(tmp_path: Path) -> None:
    engineer, qa, merge = _cycle(tmp_path)
    assert engineer["tests"] == [
        {"name": "before_repair", "status": "failed", "exit_code": 1},
        {"name": "after_repair", "status": "passed", "exit_code": 0},
    ]
    assert qa["tests"] == [{"name": "qa_fixture_test", "status": "passed", "exit_code": 0}]
    assert merge["tests"] == [{"name": "merged_fixture_test", "status": "passed", "exit_code": 0}]
    assert merge["status"] == "done"
    assert merge["merged_commit"] == engineer["commit"]
    assert merge["observed_head_commit"] == engineer["commit"]
    assert engineer["observed_head_commit"] is None
    assert qa["observed_head_commit"] is None
    assert merge["baseline_commit"] == engineer["baseline_commit"]
    assert (tmp_path / "repos" / str(merge["run_id"]) / "calculator.py").read_text() == (
        "def add_one(value: int) -> int:\n    return value + 1\n"
    )
    for receipt in (engineer, qa, merge):
        assert receipt["tokens_used"] == 0
        assert receipt["cost_usd"] == 0
        receipt_artifacts = receipt["artifacts"]
        assert isinstance(receipt_artifacts, list)
        for artifact in receipt_artifacts:
            assert isinstance(artifact, dict)
            data = (tmp_path / str(artifact["path"])).read_bytes()
            assert artifact["sha256"] == hashlib.sha256(data).hexdigest()
            assert artifact["byte_size"] == len(data)
    engineer_artifacts = engineer["artifacts"]
    assert isinstance(engineer_artifacts, list)
    diff_record = engineer_artifacts[2]
    assert isinstance(diff_record, dict)
    assert b"+    return value + 1" in (tmp_path / str(diff_record["path"])).read_bytes()


def test_two_independent_runs_can_merge_without_shared_target(tmp_path: Path) -> None:
    first = _cycle(tmp_path)[2]
    second = _cycle(tmp_path)[2]
    assert first["status"] == second["status"] == "done"
    assert first["run_id"] != second["run_id"]
    assert (tmp_path / "repos" / str(first["run_id"])).is_dir()
    assert (tmp_path / "repos" / str(second["run_id"])).is_dir()


def test_invalid_fixture_and_arbitrary_command_rejected(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    payload = json.loads(path.read_text())
    payload["fixture_id"] = "../../districtAI"
    path.write_text(json.dumps(payload))
    with pytest.raises(ContractError, match="unsupported_fixture"):
        process_job(path, tmp_path)
    payload["fixture_id"] = "engineer_qa_v1"
    payload["command"] = "cat /etc/passwd"
    path.write_text(json.dumps(payload))
    with pytest.raises(ContractError, match="invalid_manifest_fields"):
        process_job(path, tmp_path)
    assert not (tmp_path / "claims").exists()


def test_merge_requires_approval_and_exact_refs(tmp_path: Path) -> None:
    project_id, run_id = str(uuid4()), str(uuid4())
    engineer = _receipt(_manifest(tmp_path, project_id=project_id, run_id=run_id), tmp_path)
    no_approval = _manifest(tmp_path, "merge", project_id=project_id, run_id=run_id, source=engineer)
    with pytest.raises(ContractError, match="merge_requires_approval"):
        process_job(no_approval, tmp_path)
    wrong = _manifest(tmp_path, "merge", project_id=project_id, run_id=run_id, source=engineer, approval=True)
    payload = json.loads(wrong.read_text())
    payload["expected_commit"] = "0" * 40
    wrong.write_text(json.dumps(payload))
    assert _receipt(wrong, tmp_path)["status"] == "failed"
    assert not (tmp_path / "repos" / run_id).exists()


def test_changed_merge_head_fails_closed(tmp_path: Path) -> None:
    project_id, run_id = str(uuid4()), str(uuid4())
    engineer = _receipt(_manifest(tmp_path, project_id=project_id, run_id=run_id), tmp_path)
    first = _receipt(
        _manifest(tmp_path, "merge", project_id=project_id, run_id=run_id, source=engineer, approval=True),
        tmp_path,
    )
    assert first["status"] == "done"
    second = _receipt(
        _manifest(tmp_path, "merge", project_id=project_id, run_id=run_id, source=engineer, approval=True),
        tmp_path,
    )
    assert second["status"] == "failed"
    assert second["reason_code"] == "merge_target_head_changed"


def test_tampered_bundle_is_rejected_before_qa(tmp_path: Path) -> None:
    project_id, run_id = str(uuid4()), str(uuid4())
    engineer = _receipt(_manifest(tmp_path, project_id=project_id, run_id=run_id), tmp_path)
    bundle_path = tmp_path / "artifacts" / str(engineer["job_id"]) / "repair.bundle"
    bundle_path.write_bytes(bundle_path.read_bytes() + b"tampered")
    qa = _receipt(_manifest(tmp_path, "qa", project_id=project_id, run_id=run_id, source=engineer), tmp_path)
    assert qa["status"] == "failed"
    assert qa["reason_code"] == "source_bundle_hash_mismatch"


def test_post_merge_qa_failure_is_unknown_with_merged_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner import fixture_git

    project_id, run_id = str(uuid4()), str(uuid4())
    engineer = _receipt(_manifest(tmp_path, project_id=project_id, run_id=run_id), tmp_path)
    original_test = fixture_git._test

    def failed_after_merge(
        repo: Path, deadline: fixture_git.Deadline, log_limit: int, name: str
    ) -> tuple[dict[str, object], bytes]:
        if name == "merged_fixture_test":
            return {"name": name, "status": "failed", "exit_code": 1}, b"fixture failure"
        return original_test(repo, deadline, log_limit, name)

    monkeypatch.setattr(fixture_git, "_test", failed_after_merge)
    result = _receipt(
        _manifest(tmp_path, "merge", project_id=project_id, run_id=run_id, source=engineer, approval=True),
        tmp_path,
    )
    assert result["status"] == "unknown"
    assert result["observed_head_commit"] == engineer["commit"]
    head = fixture_git._git(
        tmp_path / "repos" / run_id, fixture_git.Deadline(10), "rev-parse", "HEAD"
    ).decode("ascii")
    assert head == engineer["commit"]


def test_post_merge_artifact_failure_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runner import service

    project_id, run_id = str(uuid4()), str(uuid4())
    engineer = _receipt(_manifest(tmp_path, project_id=project_id, run_id=run_id), tmp_path)

    def failed_artifact(*_args: object) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(service, "_write_artifacts", failed_artifact)
    result = _receipt(
        _manifest(tmp_path, "merge", project_id=project_id, run_id=run_id, source=engineer, approval=True),
        tmp_path,
    )
    assert result["status"] == "unknown"
    assert result["reason_code"] == "runner_io_failed"
    assert result["observed_head_commit"] == engineer["commit"]
    head = (tmp_path / "repos" / run_id / "calculator.py").read_text()
    assert "return value + 1" in head


def test_post_merge_test_timeout_retains_observed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner import fixture_git

    project_id, run_id = str(uuid4()), str(uuid4())
    engineer = _receipt(_manifest(tmp_path, project_id=project_id, run_id=run_id), tmp_path)
    original_test = fixture_git._test

    def timeout_after_readback(
        repo: Path, deadline: fixture_git.Deadline, log_limit: int, name: str
    ) -> tuple[dict[str, object], bytes]:
        if name == "merged_fixture_test":
            raise TimeoutError("synthetic timeout")
        return original_test(repo, deadline, log_limit, name)

    monkeypatch.setattr(fixture_git, "_test", timeout_after_readback)
    result = _receipt(
        _manifest(tmp_path, "merge", project_id=project_id, run_id=run_id, source=engineer, approval=True),
        tmp_path,
    )
    assert result["status"] == "unknown"
    assert result["observed_head_commit"] == engineer["commit"]


def test_merge_command_uncertain_after_side_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runner import fixture_git

    project_id, run_id = str(uuid4()), str(uuid4())
    engineer = _receipt(_manifest(tmp_path, project_id=project_id, run_id=run_id), tmp_path)
    original_git = fixture_git._git

    def uncertain_git(repo: Path, deadline: fixture_git.Deadline, *args: str) -> bytes:
        value = original_git(repo, deadline, *args)
        if args[:2] == ("merge", "--ff-only"):
            raise fixture_git.ExecutionError("synthetic_merge_response_lost")
        return value

    monkeypatch.setattr(fixture_git, "_git", uncertain_git)
    result = _receipt(
        _manifest(tmp_path, "merge", project_id=project_id, run_id=run_id, source=engineer, approval=True),
        tmp_path,
    )
    assert result["status"] == "unknown"
    assert result["reason_code"] == "merge_command_outcome_unknown"
    assert result["observed_head_commit"] == engineer["commit"]
    assert "return value + 1" in (tmp_path / "repos" / run_id / "calculator.py").read_text()


def test_unknown_merge_without_readback_has_no_observed_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner import fixture_git

    project_id, run_id = str(uuid4()), str(uuid4())
    engineer = _receipt(_manifest(tmp_path, project_id=project_id, run_id=run_id), tmp_path)
    original_git = fixture_git._git
    merge_attempted = False

    def lost_merge_and_readback(repo: Path, deadline: fixture_git.Deadline, *args: str) -> bytes:
        nonlocal merge_attempted
        if args[:2] == ("merge", "--ff-only"):
            merge_attempted = True
            raise fixture_git.ExecutionError("synthetic_merge_response_lost")
        if merge_attempted and args == ("rev-parse", "HEAD"):
            raise fixture_git.ExecutionError("synthetic_readback_lost")
        return original_git(repo, deadline, *args)

    monkeypatch.setattr(fixture_git, "_git", lost_merge_and_readback)
    result = _receipt(
        _manifest(tmp_path, "merge", project_id=project_id, run_id=run_id, source=engineer, approval=True),
        tmp_path,
    )
    assert result["status"] == "unknown"
    assert result["observed_head_commit"] is None


def test_timeout_is_unknown_and_claim_prevents_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runner import service

    path = _manifest(tmp_path, wall_seconds=1)

    def timeout(*_args: object) -> None:
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(service, "execute", timeout)
    result = _receipt(path, tmp_path)
    assert result["status"] == "unknown"
    assert result["reason_code"] == "wall_time_exceeded"
    assert process_job(path, tmp_path) is None


def test_stale_claim_does_not_rerun(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    claims = tmp_path / "claims"
    claims.mkdir()
    (claims / f"{path.stem}.claim").write_text(path.stem)
    assert process_job(path, tmp_path) is None
    assert not (tmp_path / "receipts").exists()


def test_manifest_removed_after_scan_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runner import service

    path = _manifest(tmp_path)
    original_read = service._read_manifest

    def canceled_before_open(selected: Path) -> Job:
        selected.unlink()
        return original_read(selected)

    monkeypatch.setattr(service, "_read_manifest", canceled_before_open)
    assert run_pending(tmp_path) == 0
    assert not path.exists()
    assert not (tmp_path / "claims").exists()
    assert not (tmp_path / "receipts").exists()
