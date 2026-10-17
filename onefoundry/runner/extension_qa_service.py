"""Independent reconstruction and networkless tests of the saved candidate."""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from onefoundry.extension_qa_contract import ExtensionQaJob, ExtensionQaReceipt
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid
from runner.fixture_git import Deadline, _git
from runner.isolated_fixture_test import run_isolated_fixture_test
from runner.repository_code_service import TestRunner
from runner.repository_code_stage import candidate, stage
from runner.repository_transport import publish_new


def process_qa(
    path: Path, spool: Path, source_root: Path, *, test_runner: TestRunner = run_isolated_fixture_test
) -> dict[str, Any] | None:
    if path.parent != spool / "jobs" or path.is_symlink():
        raise ValueError("extension_qa_path_invalid")
    job = ExtensionQaJob.model_validate_json(RunnerSpool._read_regular(path, 8192))
    if path.name != f"{job.job_id}.json":
        raise ValueError("extension_qa_identity_invalid")
    try:
        publish_new(spool / "claims" / f"{job.job_id}.claim", str(job.job_id).encode())
    except FileExistsError:
        return None
    checks: list[dict[str, Any]] = []
    status, reason = "failed", "extension_qa_candidate_unavailable"
    try:
        data = RunnerSpool(str(spool)).read_artifact(
            job.engineer_job.attempt_id,
            f"artifacts/{job.engineer_job.attempt_id}/change.diff",
            job.diff_byte_size,
            job.diff_sha256,
        )
        with tempfile.TemporaryDirectory(prefix="onefoundry-independent-qa-") as temporary:
            work = Path(temporary)
            repo = work / "repo"
            _, baseline = stage(job.engineer_job, source_root, repo)
            if baseline != job.baseline_commit:
                raise ValueError("extension_qa_baseline_mismatch")
            patch = work / "candidate.diff"
            patch.write_bytes(data)
            deadline = Deadline(job.engineer_job.limits["wall_seconds"])
            _git(repo, deadline, "apply", "--index", str(patch))
            observed_commit, observed_diff = candidate(job.engineer_job, repo, baseline, deadline)
            if observed_commit != job.candidate_commit or observed_diff != data:
                raise ValueError("extension_qa_candidate_mismatch")
            for index, command in enumerate(job.engineer_job.target.test_commands):
                check, _ = test_runner(
                    repo,
                    deadline,
                    job.engineer_job.limits["max_log_bytes"],
                    "qa",
                    image_id=job.engineer_job.target.test_image,
                    command=tuple(command),
                )
                checks.append({**check, "name": f"qa_{index}"})
            status = "passed" if all(check["status"] == "passed" for check in checks) else "failed"
            reason = "completed" if status == "passed" else "extension_qa_checks_failed"
    except TimeoutError:
        status, reason = "unknown", "extension_qa_timeout"
    except (OSError, ValueError, RuntimeError, SpoolInvalid):
        status, reason = "failed", "extension_qa_candidate_invalid"
    receipt = ExtensionQaReceipt.model_validate(
        {
            "job_id": str(job.job_id),
            "project_id": str(job.engineer_job.project_id),
            "request_sha256": job.request_sha256,
            "candidate_commit": job.candidate_commit,
            "diff_sha256": job.diff_sha256,
            "test_image": job.engineer_job.target.test_image,
            "status": status,
            "reason": reason,
            "tests": checks,
            "observed_at": datetime.now(UTC),
        }
    )
    value = receipt.model_dump(mode="json")
    publish_new(spool / "receipts" / f"{job.job_id}.json", json.dumps(value, sort_keys=True).encode())
    return value
